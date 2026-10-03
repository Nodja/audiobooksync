/**
 * Sync-block anchors and text-offset to DOM range mapping.
 *
 * A sidecar addresses text as `chapter.xhtml#abs-p-9`: the ninth `<p>` of that document that holds
 * readable text. The epub is never modified, so the reader repeats the generator's walk to find
 * the same element. The two walks must agree, so a change here has to be mirrored in
 * `audiobooksync/epub.py`.
 */

// Kept identical to SYNC_BLOCK_TAGS in the generator.
export const SYNC_BLOCK_TAGS = new Set([
  'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'dd', 'dt', 'td', 'th', 'figcaption',
  'caption', 'blockquote', 'pre', 'address', 'summary', 'div', 'section', 'article', 'aside',
  'header', 'footer', 'main', 'nav'
])

export const INLINE_TAGS = new Set([
  'a', 'span', 'em', 'strong', 'i', 'b', 'u', 'small', 'sub', 'sup', 'code', 'q', 'cite',
  'abbr', 'time', 'mark', 's', 'ins', 'del', 'big', 'tt', 'font', 'label', 'bdi', 'bdo',
  'ruby', 'rt', 'rp', 'wbr'
])

export const SKIP_TAGS = new Set([
  'script', 'style', 'head', 'title', 'meta', 'link', 'svg', 'math', 'template'
])

const isBlock = (el) => SYNC_BLOCK_TAGS.has(el.localName) && !INLINE_TAGS.has(el.localName)

const isSkipped = (el) => SKIP_TAGS.has(el.localName)

// `childNodes` is a live `NodeList` that is not iterable in every WebView, and this walk is hot.
const visit = (node, fn) => {
  const children = node && node.childNodes
  if (!children) return
  for (let i = 0; i < children.length; i++) fn(children[i])
}

/** Matches `block_anchor()` in audiobooksync/epub.py. */
export const blockAnchor = (tag, counter) => `abs-${tag}-${counter}`

/**
 * The blocks of one document that hold text, in document order.
 *
 * Text belongs to the innermost open block, so a `<div>` that only wraps other blocks takes no
 * anchor number. Skipping empty elements when numbering is what keeps the counters equal to the
 * generator's.
 */
export function collectSyncBlocks(root) {
  const counters = Object.create(null)
  const blocks = []
  const stack = []

  const currentBlock = () => (stack.length ? stack[stack.length - 1] : null)

  const flushText = (text) => {
    if (!text) return
    const target = currentBlock()
    if (!target) return
    // Placed on the first run of text, not at close, so a container comes before the blocks nested
    // in it: `<li>text<ul><li>sub</li></ul></li>` reads the item, then the sublist.
    if (!target.placed) {
      target.placed = true
      counters[target.tag] = (counters[target.tag] || 0) + 1
      target.anchor = target.id || blockAnchor(target.tag, counters[target.tag])
      blocks.push(target)
    }
    target.parts.push(text)
  }

  const walk = (node) => {
    visit(node, (child) => {
      if (child.nodeType === 3) {
        flushText(child.data)
      } else if (child.nodeType === 1) {
        if (isSkipped(child)) return
        if (child.localName === 'br') {
          flushText(' ')
          return
        }
        const frame = {
          element: child,
          tag: child.localName,
          parts: [],
          id: (child.getAttribute('id') || '').trim() || null
        }
        const block = isBlock(child)
        if (block) stack.push(frame)
        walk(child)
        if (block) stack.pop()
      }
    })
  }

  walk(root.documentElement || root)
  return blocks.map((frame) => ({
    anchor: frame.anchor,
    element: frame.element,
    tag: frame.tag,
    text: frame.parts.join('')
  }))
}

// Highlighting asks on every word and the walk is the expensive part. Highlights are painted in an
// overlay, so a document is never mutated and a reloaded section is a new key.
const blockIndexes = new WeakMap()

function blockIndex(root) {
  let index = blockIndexes.get(root)
  if (!index) {
    index = { byAnchor: new Map(), byElement: new Map() }
    for (const block of collectSyncBlocks(root)) {
      if (!index.byAnchor.has(block.anchor)) index.byAnchor.set(block.anchor, block)
      index.byElement.set(block.element, block)
    }
    blockIndexes.set(root, index)
  }
  return index
}

/** The block with this anchor, or `null`. */
export function findSyncBlock(root, anchor) {
  return blockIndex(root).byAnchor.get(anchor) || null
}

/** The sync block that owns a node's text, or `null`. */
export function blockOfNode(root, node) {
  const { byElement } = blockIndex(root)
  for (let el = node.nodeType === 1 ? node : node.parentNode; el && el.nodeType === 1; el = el.parentNode) {
    if (isSkipped(el)) return null
    if (isBlock(el)) return byElement.get(el) || null
  }
  return null
}

/** A block's text as the generator joins it, with the text node behind each stretch. */
export function blockTextSegments(block) {
  const nodes = []
  let text = ''
  const walk = (node) => {
    visit(node, (child) => {
      if (child.nodeType === 3) {
        nodes.push({ node: child, start: text.length, end: text.length + child.data.length })
        text += child.data
      } else if (child.nodeType === 1 && !isSkipped(child)) {
        // The generator separates words at a `<br/>` too. The space has no text node, which costs
        // at most one character of highlight.
        if (child.localName === 'br') text += ' '
        // A nested block owns its own text; descending would shift every later timing.
        else if (!isBlock(child)) walk(child)
      }
    })
  }
  walk(block)
  return { text, nodes }
}

const caretAt = (doc, x, y) => {
  if (doc.caretPositionFromPoint) {
    const position = doc.caretPositionFromPoint(x, y)
    return position ? { node: position.offsetNode, offset: position.offset } : null
  }
  if (doc.caretRangeFromPoint) {
    const range = doc.caretRangeFromPoint(x, y)
    return range ? { node: range.startContainer, offset: range.startOffset } : null
  }
  return null
}

const rectsContain = (rects, x, y, slack) => {
  for (let i = 0; i < rects.length; i++) {
    const r = rects[i]
    if (x >= r.left - slack && x <= r.right + slack && y >= r.top - slack && y <= r.bottom + slack) return true
  }
  return false
}

/**
 * The word under a point, as `{block, index}`, or `null` off the text.
 *
 * The caret snaps to the nearest text even from blank space, so the word's own rectangles are
 * checked against the point.
 */
export function wordAtPoint(doc, x, y, slack = 3) {
  const caret = caretAt(doc, x, y)
  if (!caret || !caret.node || caret.node.nodeType !== 3) return null
  const block = blockOfNode(doc, caret.node)
  if (!block) return null
  const segment = blockTextSegments(block.element).nodes.find((s) => s.node === caret.node)
  if (!segment) return null

  const at = segment.start + caret.offset
  const tokens = wordOffsets(block.element)
  const inside = tokens.findIndex((tok) => tok.start <= at && at < tok.end)
  const justBefore = tokens.findIndex((tok) => tok.end === at)
  for (const index of [inside, justBefore]) {
    if (index < 0) continue
    const range = rangeForOffsets(block.element, tokens[index].start, tokens[index].end)
    if (range && rectsContain(range.getClientRects(), x, y, slack)) return { block, index }
  }
  return null
}

/** A `Range` over `[start, end)` of a block's text, which may span several inline elements. */
export function rangeForOffsets(block, start, end) {
  const { nodes } = blockTextSegments(block)
  if (!nodes.length) return null
  let startNode = null
  let startOffset = 0
  let endNode = null
  let endOffset = 0
  for (const seg of nodes) {
    if (startNode === null && start < seg.end) {
      startNode = seg.node
      startOffset = Math.max(0, start - seg.start)
    }
    if (end <= seg.end) {
      endNode = seg.node
      endOffset = Math.max(0, end - seg.start)
      break
    }
  }
  if (startNode === null) {
    const last = nodes[nodes.length - 1]
    startNode = last.node
    startOffset = last.node.data.length
  }
  if (endNode === null) {
    const last = nodes[nodes.length - 1]
    endNode = last.node
    endOffset = last.node.data.length
  }
  const doc = block.ownerDocument
  const range = doc.createRange()
  try {
    range.setStart(startNode, Math.min(startOffset, startNode.data.length))
    range.setEnd(endNode, Math.min(endOffset, endNode.data.length))
  } catch (e) {
    return null
  }
  return range
}

/** Character offsets of each word in a block's text; the nth timing belongs to the nth token. */
export function wordOffsets(block) {
  const { text } = blockTextSegments(block)
  const out = []
  // Hyphens and dashes split as well as whitespace, as in the generator.
  const re = /[^\s\u2010-\u2015\u2212-]+/g
  let m
  while ((m = re.exec(text)) !== null) {
    // The generator drops tokens that normalise to nothing (a lone `”`, bullet or ellipsis).
    if (normalizeWord(m[0])) out.push({ start: m.index, end: m.index + m[0].length, text: m[0] })
  }
  return out
}

/** Normalise a token the way the generator does. */
export function normalizeWord(token) {
  return String(token)
    .replace(/[\u2018\u2019\u02bc]/g, "'")
    .replace(/[\u201c\u201d]/g, '"')
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9'\u00c0-\u024f\u0400-\u04ff\u3040-\u30ff\u4e00-\u9fff]+/g, '')
}

const decodeHref = (value) => {
  try {
    return decodeURIComponent(value)
  } catch (e) {
    return value
  }
}

/** Bare `[start, end]` pairs, or `{start, end, text}` objects as `--word-text` writes. */
function decodeWordTimings(raw) {
  try {
    const parsed = JSON.parse(raw || '[]')
    if (!Array.isArray(parsed)) return []
    return parsed
      .map((w) => {
        if (Array.isArray(w)) {
          return { text: '', start: w[0], end: w.length > 1 ? w[1] : w[0] + 0.08 }
        }
        if (w && typeof w === 'object') {
          return {
            text: typeof w.text === 'string' ? w.text : '',
            start: w.start,
            end: Number.isFinite(w.end) ? w.end : w.start + 0.08
          }
        }
        return null
      })
      .filter((w) => w && Number.isFinite(w.start))
  } catch (e) {
    console.warn('[abs-anchors] ignoring malformed word_timestamps', e)
    return []
  }
}

/**
 * Parse a `.smil` sidecar into time-ordered cues, one per `<par>`.
 *
 * Word timings carry no character offsets, so the nth timing is matched to the nth token of its
 * block once the block is loaded (see `attachCueText`).
 */
export function parseSmil(source) {
  const doc = typeof source === 'string'
    ? new DOMParser().parseFromString(source, 'application/xml')
    : source
  if (!doc) return { cues: [], audioSrc: null, duration: 0 }

  const parseClock = (value) => {
    if (!value) return null
    const str = String(value).trim()
    if (/^\d+(\.\d+)?(h|min|ms|s)$/.test(str) || /^\d+(\.\d+)?$/.test(str)) {
      const [, x, unit] = /^([\d.]+)(h|min|ms|s)?$/.exec(str)
      const n = parseFloat(x)
      if (!Number.isFinite(n)) return null
      if (unit === 'h') return n * 3600
      if (unit === 'min') return n * 60
      if (unit === 'ms') return n / 1000
      return n
    }
    const parts = str.split(':').map(Number)
    if (parts.some((p) => !Number.isFinite(p))) return null
    return parts.reduce((acc, part) => acc * 60 + part, 0)
  }

  // Matches the qualified name as written, which is what the generator emits.
  const pars = doc.getElementsByTagName('par')
  const cues = []
  let audioSrc = null

  for (let i = 0; i < pars.length; i++) {
    const par = pars[i]
    const textEl = par.getElementsByTagName('text')[0]
    const audioEl = par.getElementsByTagName('audio')[0]
    const src = textEl && textEl.getAttribute('src')
    if (!src) continue

    const begin = audioEl ? parseClock(audioEl.getAttribute('clipBegin')) : null
    const end = audioEl ? parseClock(audioEl.getAttribute('clipEnd')) : null
    if (audioSrc === null && audioEl) audioSrc = audioEl.getAttribute('src')

    const hash = src.indexOf('#')
    const href = hash < 0 ? src : src.slice(0, hash)
    const anchor = hash < 0 ? null : decodeHref(src.slice(hash + 1))

    // Decoded on first use: a book has tens of thousands of cues and few are ever read.
    let rawWords = null
    // Where this cue's words begin in its block's word list, since a paragraph of several
    // sentences is several cues.
    let wordOffset = 0
    // Older sidecars carry the timings in a `<meta>` child of `<par>` instead of `data-*`.
    const offset = par.getAttribute('data-word-offset')
    if (offset !== null) {
      const parsed = parseInt(offset, 10)
      if (Number.isFinite(parsed) && parsed > 0) wordOffset = parsed
    }
    const payload = par.getAttribute('data-word-timestamps')
    if (payload !== null) {
      rawWords = payload
    } else {
      const metas = par.getElementsByTagName('meta')
      for (let m = 0; m < metas.length; m++) {
        const name = metas[m].getAttribute('name')
        if (name === 'word_offset') {
          const parsed = parseInt(metas[m].getAttribute('content') || '0', 10)
          if (Number.isFinite(parsed) && parsed > 0) wordOffset = parsed
          continue
        }
        if (name !== 'word_timestamps') continue
        rawWords = metas[m].getAttribute('content')
        break
      }
    }

    cues.push({
      index: cues.length,
      href: decodeHref(href),
      anchor,
      begin: begin === null ? 0 : begin,
      end: end === null ? (begin === null ? 0 : begin) : end,
      rawWords,
      words: null,
      wordOffset,
      wordOffsets: null
    })
  }

  const totals = doc.getElementsByTagName('meta')
  let duration = 0
  for (let i = 0; i < totals.length; i++) {
    if (totals[i].getAttribute('name') === 'totalDuration') {
      duration = parseClock(totals[i].getAttribute('content')) || 0
    }
  }

  cues.sort((a, b) => a.begin - b.begin)
  cues.forEach((cue, i) => { cue.index = i })
  return { cues, audioSrc, duration }
}

export function cueWords(cue) {
  if (!cue.words) {
    cue.words = decodeWordTimings(cue.rawWords)
    cue.rawWords = null
  }
  return cue.words
}

/**
 * Pair a cue's word timings with the character offsets of its block.
 *
 * Pairing starts at `cue.wordOffset`, the cue's slice of the block. Timing text is ignored on
 * purpose: the aligner works on normalised tokens, which differ from the display text.
 */
export function attachCueText(cue, block) {
  if (!block) return null
  if (cue.wordOffsets) return cue.wordOffsets
  const tokens = wordOffsets(block)
  const base = Number.isFinite(cue.wordOffset) && cue.wordOffset > 0 ? cue.wordOffset : 0
  const words = cueWords(cue)
  const offsets = []
  for (let i = 0; i < words.length; i++) {
    const token = tokens[base + i]
    // The block has fewer tokens than the cue has timings: a real mismatch, so stop.
    if (!token) break
    offsets.push({
      start: token.start,
      end: token.end,
      begin: words[i].start,
      stop: words[i].end
    })
  }
  cue.wordOffsets = offsets
  return offsets
}

/** Group cues by the block they narrate, keyed `<href>#<anchor>`. */
export function indexCuesByBlock(cues, normaliseHref) {
  const byBlock = new Map()
  for (const cue of cues) {
    const key = `${normaliseHref(cue.href)}#${cue.anchor}`
    const list = byBlock.get(key)
    if (list) list.push(cue)
    else byBlock.set(key, [cue])
  }
  return byBlock
}

/** When the narration reaches word `index` of a block, or `null` for a word the aligner skipped. */
export function seekTimeForWord(cues, index) {
  for (const cue of cues || []) {
    const words = cueWords(cue)
    const local = index - (cue.wordOffset > 0 ? cue.wordOffset : 0)
    if (local >= 0 && local < words.length) return words[local].start
  }
  return null
}

/** Index cues for lookup by playback position, which is queried several times a second. */
export function buildCueIndex(cues) {
  const begins = cues.map((c) => c.begin)
  return {
    cues,
    begins,
    /** The last cue that starts at or before `time`. */
    at(time) {
      let lo = 0
      let hi = begins.length - 1
      let found = -1
      while (lo <= hi) {
        const mid = (lo + hi) >> 1
        if (begins[mid] <= time) {
          found = mid
          lo = mid + 1
        } else {
          hi = mid - 1
        }
      }
      return found < 0 ? null : cues[found]
    }
  }
}

/**
 * The index of the word being spoken at `time`, or `-1`.
 *
 * A word stays current until the next starts, so the highlight does not flicker in the gaps.
 */
export function wordAt(cue, time) {
  const offsets = cue.wordOffsets
  if (!offsets || !offsets.length) return -1
  if (time < offsets[0].begin) return -1
  let lo = 0
  let hi = offsets.length - 1
  let found = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (offsets[mid].begin <= time) {
      found = mid
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  return found
}
