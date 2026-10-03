/**
 * Offline copies of the sync sidecars of downloaded books, in IndexedDB.
 *
 * A sidecar is up to ~20 MB of text, so it is stored gzipped. Entries are evicted least recently
 * used past `LIMIT_BYTES`, never the one just stored. The browser may drop the whole database, so a
 * miss falls back to fetching. No failure here may break reading.
 */

const DB_NAME = 'abs-sync-sidecars'
const LIMIT_BYTES = 15 * 1024 * 1024

const META = 'meta' // key -> { key, ino, size, mtimeMs, bytes, encoding, lastUsed }
const DATA = 'data' // key -> Blob

const supportsGzip = () => typeof CompressionStream === 'function' && typeof DecompressionStream === 'function'

const sameVersion = (a, b) => !!a && !!b && a.ino === b.ino && a.size === b.size && a.mtimeMs === b.mtimeMs

async function encode(text) {
  if (!supportsGzip()) return { blob: new Blob([text]), encoding: 'identity' }
  const blob = await new Response(new Blob([text]).stream().pipeThrough(new CompressionStream('gzip'))).blob()
  return { blob, encoding: 'gzip' }
}

async function decode(blob, encoding) {
  if (encoding === 'gzip') return new Response(blob.stream().pipeThrough(new DecompressionStream('gzip'))).text()
  return blob.text()
}

export function createSidecarCache({ limitBytes = LIMIT_BYTES, name = DB_NAME, now = () => Date.now() } = {}) {
  let opening = null

  const open = () => {
    if (!opening) {
      opening = new Promise((resolve, reject) => {
        const request = indexedDB.open(name, 1)
        request.onupgradeneeded = () => {
          request.result.createObjectStore(META, { keyPath: 'key' })
          request.result.createObjectStore(DATA)
        }
        request.onsuccess = () => resolve(request.result)
        request.onerror = () => reject(request.error)
      })
      opening.catch(() => {
        opening = null
      })
    }
    return opening
  }

  /** Resolves with `work`'s return value once the transaction commits. */
  const transact = async (mode, work) => {
    const db = await open()
    return new Promise((resolve, reject) => {
      const tx = db.transaction([META, DATA], mode)
      let result
      tx.oncomplete = () => resolve(result)
      tx.onerror = () => reject(tx.error)
      tx.onabort = () => reject(tx.error)
      result = work(tx.objectStore(META), tx.objectStore(DATA), tx)
    })
  }

  const request = (req) =>
    new Promise((resolve, reject) => {
      req.onsuccess = () => resolve(req.result)
      req.onerror = () => reject(req.error)
    })

  async function load(key) {
    const found = await transact('readwrite', (meta, data) => {
      const holder = {}
      meta.get(key).onsuccess = (event) => {
        const entry = event.target.result
        if (!entry) return
        holder.meta = entry
        data.get(key).onsuccess = (inner) => {
          holder.blob = inner.target.result
          if (holder.blob) meta.put({ ...entry, lastUsed: now() })
        }
      }
      return holder
    })
    if (!found.meta || !found.blob) return null
    return { meta: found.meta, text: await decode(found.blob, found.meta.encoding) }
  }

  async function store(key, version, text) {
    const { blob, encoding } = await encode(text)
    // An entry that cannot fit even alone is not worth evicting everything else for.
    if (blob.size > limitBytes) {
      await remove(key)
      return false
    }
    await transact('readwrite', (meta, data) => {
      data.put(blob, key)
      meta.put({ key, ino: version.ino, size: version.size, mtimeMs: version.mtimeMs, bytes: blob.size, encoding, lastUsed: now() })
      meta.getAll().onsuccess = (event) => {
        const others = event.target.result.filter((entry) => entry.key !== key).sort((a, b) => a.lastUsed - b.lastUsed)
        let total = blob.size + others.reduce((sum, entry) => sum + entry.bytes, 0)
        for (const oldest of others) {
          if (total <= limitBytes) break
          meta.delete(oldest.key)
          data.delete(oldest.key)
          total -= oldest.bytes
        }
      }
    })
    return true
  }

  async function remove(key) {
    try {
      await transact('readwrite', (meta, data) => {
        meta.delete(key)
        data.delete(key)
      })
    } catch (error) {
      console.warn('[sidecarCache] remove failed', error)
    }
  }

  /** Everything stored, oldest use first. */
  async function entries() {
    const db = await open()
    const all = await request(db.transaction(META).objectStore(META).getAll())
    return all.sort((a, b) => a.lastUsed - b.lastUsed)
  }

  /**
   * The sidecar text from the cache when it is current, else from `fetchText`. `remote` is the
   * server's current file entry, or null when the server could not be asked, in which case the
   * cached copy is used as it is. Null when there is nothing to use.
   */
  async function resolve({ key, remote, fetchText }) {
    let cached = null
    try {
      cached = await load(key)
    } catch (error) {
      console.warn('[sidecarCache] read failed', error)
    }
    if (!remote) return cached ? cached.text : null
    if (cached && sameVersion(cached.meta, remote)) return cached.text

    const text = await fetchText()
    try {
      await store(key, remote, text)
    } catch (error) {
      console.warn('[sidecarCache] write failed', error)
    }
    return text
  }

  return { load, store, remove, entries, resolve }
}

export const sidecarCache = createSidecarCache()
