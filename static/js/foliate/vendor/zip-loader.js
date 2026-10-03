/**
 * Minimal EPUB loader for foliate-js.
 *
 * foliate-js expects a loader of the shape `{ entries, loadText(name), loadBlob(name), getSize(name) }`,
 * normally produced by zip.js. Audiobookshelf's ebook endpoint serves the epub as one opaque file,
 * so the archive has to be opened in the browser, and the obvious route — vendoring zip.js — is a
 * large dependency (`makeZipLoader` in view.js even points at a file that is not vendored).
 *
 * Instead this reads the zip's central directory directly and inflates entries on demand. That
 * gives up streaming and encryption support, neither of which an epub needs: entries are inflated
 * lazily and cached, so a 2 MB epub does not become a 2 MB set of strings for chapters nobody reads.
 *
 * Inflation has two paths. The platform's `DecompressionStream('deflate-raw')` is preferred where
 * it exists, because it is native and off the main thread. Where it does not — it is Safari 16.4+
 * and iOS 16.4+ — the vendored fflate's `inflateSync` takes over. Without that fallback an older
 * WebView cannot open a book at all, which is a much worse failure than a slower one.
 *
 * The dynamic-import path in `view.js` (`./vendor/zip.js`) is deliberately left alone; the reader
 * calls this directly and hands foliate-js a ready-made loader.
 */

import { inflateSync } from './fflate.js'

const EOCD_SIGNATURE = 0x06054b50
const CENTRAL_SIGNATURE = 0x02014b50
const LOCAL_SIGNATURE = 0x04034b50
const MAX_COMMENT = 0xffff

/** Concatenate the chunks of a stream into one buffer. */
async function collect(stream) {
  const reader = stream.getReader()
  const chunks = []
  let total = 0
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    chunks.push(value)
    total += value.length
  }
  const out = new Uint8Array(total)
  let offset = 0
  for (const chunk of chunks) {
    out.set(chunk, offset)
    offset += chunk.length
  }
  return out
}

const hasNativeInflate = typeof DecompressionStream !== 'undefined'

/**
 * Inflate a raw DEFLATE stream.
 *
 * `deflate-raw` is the correct format here — zip entries carry no zlib wrapper.
 */
async function inflate(data) {
  if (hasNativeInflate) {
    try {
      return await collect(
        new Blob([data]).stream().pipeThrough(new DecompressionStream('deflate-raw'))
      )
    } catch (error) {
      throw new Error(`Failed to decompress epub entry: ${error.message}`)
    }
  }
  try {
    return inflateSync(data)
  } catch (error) {
    throw new Error(`Failed to decompress epub entry: ${error.message}`)
  }
}

/** Decode a filename, preferring the UTF-8 flag and falling back to the byte range. */
function decodeName(bytes, utf8) {
  if (utf8) return new TextDecoder('utf-8').decode(bytes)
  return new TextDecoder('utf-8').decode(bytes)
}

/**
 * Read the central directory of a zip archive.
 *
 * @param {Uint8Array} bytes whole archive
 * @returns {Map<string, object>} entry name → entry record
 */
function readCentralDirectory(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)

  // The end-of-central-directory record sits at the end, after a comment of unknown length, so
  // it is found by scanning backwards over the largest possible comment.
  let eocd = -1
  const lowest = Math.max(0, bytes.length - MAX_COMMENT - 22)
  for (let i = bytes.length - 22; i >= lowest; i--) {
    if (view.getUint32(i, true) === EOCD_SIGNATURE) {
      eocd = i
      break
    }
  }
  if (eocd < 0) throw new Error('not a zip file: no end-of-central-directory record')

  let count = view.getUint16(eocd + 10, true)
  let offset = view.getUint32(eocd + 16, true)

  // Zip64 archives store 0xffff/0xffffffff here; epubs never do, but failing loudly beats
  // reading garbage.
  if (count === 0xffff || offset === 0xffffffff) {
    throw new Error('zip64 archives are not supported')
  }

  const entries = new Map()
  for (let n = 0; n < count; n++) {
    if (view.getUint32(offset, true) !== CENTRAL_SIGNATURE) {
      throw new Error(`corrupt zip: bad central directory entry ${n}`)
    }
    const flags = view.getUint16(offset + 8, true)
    const method = view.getUint16(offset + 10, true)
    const compressedSize = view.getUint32(offset + 20, true)
    const uncompressedSize = view.getUint32(offset + 24, true)
    const nameLength = view.getUint16(offset + 28, true)
    const extraLength = view.getUint16(offset + 30, true)
    const commentLength = view.getUint16(offset + 32, true)
    const localOffset = view.getUint32(offset + 42, true)
    const name = decodeName(bytes.subarray(offset + 46, offset + 46 + nameLength), Boolean(flags & 0x800))

    entries.set(name, {
      name,
      method,
      compressedSize,
      uncompressedSize,
      localOffset,
      encrypted: Boolean(flags & 0x1)
    })
    offset += 46 + nameLength + extraLength + commentLength
  }
  return entries
}

/** Read and inflate one entry, following its local header for the true data offset. */
async function readEntry(bytes, view, entry) {
  if (entry.encrypted) throw new Error(`encrypted epub entry: ${entry.name}`)
  if (view.getUint32(entry.localOffset, true) !== LOCAL_SIGNATURE) {
    throw new Error(`corrupt zip: bad local header for ${entry.name}`)
  }
  // The local header repeats the name and extra field, and its lengths can differ from the
  // central directory's, so the payload offset must come from the local header.
  const nameLength = view.getUint16(entry.localOffset + 26, true)
  const extraLength = view.getUint16(entry.localOffset + 28, true)
  const start = entry.localOffset + 30 + nameLength + extraLength
  const stored = bytes.subarray(start, start + entry.compressedSize)

  if (entry.method === 0) return stored
  if (entry.method === 8) return inflate(stored)
  throw new Error(`unsupported compression method ${entry.method} for ${entry.name}`)
}

/**
 * Build a foliate-js loader over an epub's bytes.
 *
 * @param {Uint8Array} bytes the whole epub file
 * @returns {Promise<{entries: object[], loadText: Function, loadBlob: Function, getSize: Function}>}
 */
export async function makeZipLoader(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const index = readCentralDirectory(bytes)
  const cache = new Map()

  // Inflated entries are kept, because a chapter is asked for repeatedly (its text, then each of
  // its images and stylesheets). Blobs are cheap to recreate, so only text is cached by value.
  const data = async (name) => {
    const entry = index.get(name)
    if (!entry) return null
    if (cache.has(name)) return cache.get(name)
    const payload = readEntry(bytes, view, entry)
    cache.set(name, payload)
    return payload
  }

  const textCache = new Map()
  const decoder = new TextDecoder('utf-8')

  const loadText = async (name) => {
    if (textCache.has(name)) return textCache.get(name)
    const payload = await data(name)
    if (payload === null) return null
    const text = decoder.decode(payload)
    textCache.set(name, text)
    return text
  }

  const loadBlob = async (name, type) => {
    const payload = await data(name)
    return payload === null ? null : new Blob([payload], type ? { type } : undefined)
  }

  const getSize = (name) => index.get(name)?.uncompressedSize ?? 0

  return {
    entries: Array.from(index.values()),
    loadText,
    loadBlob,
    getSize
  }
}

/** Names of the entries in an archive, for diagnostics. */
export function listEntries(loader) {
  return loader.entries.map((entry) => entry.name)
}
