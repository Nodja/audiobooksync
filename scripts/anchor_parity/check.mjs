// Checks that the client's sync-block walk (abs-anchors.js) agrees with the generator's
// (audiobooksync/epub.py): same anchors, same block text, same number of words per block.  A
// disagreement puts highlights on the wrong paragraph or the wrong word.
//
//   node scripts/anchor_parity/check.mjs <folder with epubs>
//
// Needs `uv` for the generator side and `@xmldom/xmldom` from the web client's node_modules.
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { spawnSync } from 'node:child_process'
import { pathToFileURL } from 'node:url'

const root = path.resolve(import.meta.dirname, '../..')
const library = process.argv[2]
if (!library) {
  console.error('usage: node scripts/anchor_parity/check.mjs <folder with epubs>')
  process.exit(2)
}

const { DOMParser } = createRequire(path.join(root, 'audiobookshelf/client/'))('@xmldom/xmldom')
const anchors = await import(pathToFileURL(path.join(root, 'audiobookshelf/client/static/js/foliate/abs-anchors.js')))

const out = fs.mkdtempSync(path.join(os.tmpdir(), 'anchor-parity-'))
const dump = spawnSync('uv', ['run', 'python', path.join(root, 'scripts/anchor_parity/dump.py'), out, library], {
  cwd: root,
  stdio: ['ignore', 'inherit', 'inherit']
})
if (dump.status !== 0) process.exit(dump.status || 1)

let failed = 0
for (const book of JSON.parse(fs.readFileSync(path.join(out, 'books.json'), 'utf8'))) {
  const expected = JSON.parse(fs.readFileSync(path.join(out, book.dir, 'py.json'), 'utf8'))
  const byDocument = new Map()
  for (const [href, anchor, words, text] of expected.blocks) {
    if (!byDocument.has(href)) byDocument.set(href, [])
    byDocument.get(href).push({ anchor, words, text })
  }

  const counts = { blocks: 0, missing: 0, text: 0, words: 0 }
  let example = null
  for (const href of expected.hrefs) {
    const markup = fs.readFileSync(path.join(out, book.dir, 'docs', href.replace(/\//g, '__')), 'utf8')
    const doc = new DOMParser({ errorHandler: () => {} }).parseFromString(markup, 'application/xhtml+xml')
    const found = new Map(anchors.collectSyncBlocks(doc).map((b) => [b.anchor, b]))
    for (const want of byDocument.get(href)) {
      counts.blocks++
      const got = found.get(want.anchor)
      if (!got) {
        counts.missing++
        example ??= `${href}#${want.anchor} is missing`
        continue
      }
      if (got.text !== want.text) counts.text++
      const words = anchors.wordOffsets(got.element).length
      if (words !== want.words) {
        counts.words++
        example ??= `${href}#${want.anchor}: ${words} words here, ${want.words} in the generator: ${JSON.stringify(got.text.slice(0, 60))}`
      }
    }
  }
  const bad = counts.missing + counts.text + counts.words
  if (bad) failed++
  console.log(`${bad ? 'FAIL' : 'ok  '} ${book.name}  ${counts.blocks} blocks` + (bad ? `  missing ${counts.missing}, text ${counts.text}, words ${counts.words}  e.g. ${example}` : ''))
}
fs.rmSync(out, { recursive: true, force: true })
process.exit(failed ? 1 : 0)
