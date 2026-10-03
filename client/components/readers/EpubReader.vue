<template>
  <div id="epub-reader" class="h-full w-full flex flex-col">
    <!-- Top strip stays empty: Reader.vue floats the book title there and foliate paints over anything it overlaps. -->
    <div class="relative grow min-h-0 pt-11">
      <button v-if="view" type="button" aria-label="Previous page" class="absolute left-0 top-0 z-10 h-full w-16 hidden sm:flex items-center justify-center opacity-40 hover:opacity-100" @click="prev">
        <span class="material-symbols text-5xl">chevron_left</span>
      </button>

      <div ref="frame" id="frame" class="h-full w-full overflow-hidden"></div>

      <button v-if="view" type="button" aria-label="Next page" class="absolute right-0 top-0 z-10 h-full w-16 hidden sm:flex items-center justify-center opacity-40 hover:opacity-100" @click="next">
        <span class="material-symbols text-5xl">chevron_right</span>
      </button>

      <div v-if="loading" class="absolute inset-0 flex items-center justify-center bg-black/30">
        <p class="text-sm opacity-80">{{ loadingMessage }}</p>
      </div>
    </div>
  </div>
</template>

<script>
/**
 * EPUB reader backed by foliate-js, with audiobook read-along.
 *
 * A sidecar addresses a sentence as `chapter.xhtml#abs-p-7`; `abs-anchors.js` repeats the
 * generator's numbering to find it. The audio player owns playback and this component polls its
 * clock, turning the page when the spoken sentence is off screen.
 */

// Plain ES modules in `client/static`, served from the site root.
const FOLIATE_VIEW = '/js/foliate/view.js'
const FOLIATE_ANCHORS = '/js/foliate/abs-anchors.js'
const FOLIATE_ZIP = '/js/foliate/vendor/zip-loader.js'
const FOLIATE_EPUB = '/js/foliate/epub.js'

// The vendored foliate files must not be bundled, and webpack 4 has no `webpackIgnore`. Building the
// import through `new Function` hides it from webpack but stays a real browser `import()`.
const runtimeImport = new Function('url', 'return import(url)')

/** Minimum gap between automatic section changes (seeks are exempt). */
const SECTION_TURN_GAP_MS = 1200
/** A clock jump bigger than this between two polls is a seek, not playback. */
const SEEK_JUMP_SECONDS = 5
/** Playback clock sampling interval; a native `timeupdate` (~250 ms) is too coarse for word highlighting. */
const SYNC_POLL_MS = 60

const MAX_SEARCH_RESULTS_PER_SECTION = 40

// Overlayer keeps one layer per key, so re-adding under the same key replaces the highlight.
const SENTENCE_KEY = 'abs-sync-sentence'
const WORD_KEY = 'abs-sync-word'
const SENTENCE_COLOR = '#5b8def'
const SENTENCE_OPACITY = 0.28
const WORD_COLOR = '#f0a020'
const WORD_RULE_WIDTH = 2

const SVG_NS = 'http://www.w3.org/2000/svg'

/** Section documents that already carry the click handler; foliate may announce one more than once. */
const clickWired = new WeakSet()

function svgRects(rects, fill, opacity, toRectAttrs) {
  const g = document.createElementNS(SVG_NS, 'g')
  g.setAttribute('fill', fill)
  if (opacity != null) g.setAttribute('opacity', String(opacity))
  for (const r of rects) {
    const rect = document.createElementNS(SVG_NS, 'rect')
    for (const [name, value] of Object.entries(toRectAttrs(r))) rect.setAttribute(name, String(value))
    g.appendChild(rect)
  }
  return g
}

function drawHighlight(rects, { color = SENTENCE_COLOR, opacity = SENTENCE_OPACITY } = {}) {
  return svgRects(rects, color, opacity, ({ left, top, width, height }) => ({ x: left, y: top, width, height }))
}

function drawUnderline(rects, { color = WORD_COLOR, width = WORD_RULE_WIDTH } = {}) {
  return svgRects(rects, color, null, ({ left, bottom, width: span }) => ({ x: left, y: bottom - width, width: span, height: width }))
}

export default {
  props: {
    libraryItem: {
      type: Object,
      default: () => ({})
    },
    playerOpen: Boolean,
    keepProgress: Boolean,
    fileId: String
  },
  data() {
    return {
      loading: true,
      loadingMessage: 'Loading ebook…',
      windowWidth: 0,
      book: null,
      view: null,
      anchors: null,
      chapters: [],
      currentLocationCfi: null,
      onRelocateListener: null,
      resizeTimer: null,

      syncCues: null,
      syncIndex: null,
      blockCues: null,
      syncEnabled: false,
      syncTimer: null,
      // A standing problem with this book's sidecar, empty when there is none.
      syncStatus: '',
      // Why following is idle right now; recomputed every poll.
      syncBlocker: '',
      syncCueIndex: -1,
      syncWordIndex: -1,
      // What is painted, as against requested above. They differ while a page turn is deferred,
      // which is what makes the poll retry.
      syncPaintedCue: -1,
      syncPaintedWord: -1,
      lastSyncTime: -1,
      syncLastJump: 0,
      // True while revealRange moves the renderer; its `load` event must not re-enter.
      syncNavigating: false,
      syncTurning: false,

      ereaderSettings: {
        theme: 'dark',
        font: 'serif',
        fontScale: 100,
        lineSpacing: 115,
        spread: 'auto',
        flow: 'paginated',
        textStroke: 0,
        sync: true,
        clickToSeek: true
      }
    }
  },
  computed: {
    libraryItemId() {
      return this.libraryItem?.id
    },
    libraryFiles() {
      return this.libraryItem?.libraryFiles || []
    },
    smilFile() {
      return this.libraryFiles.find((lf) => /\.smil$/i.test(lf?.metadata?.filename || '')) || null
    },
    hasSyncTrack() {
      return !!this.syncCues?.length
    },
    syncStatusText() {
      if (this.ereaderSettings.sync === false) return ''
      return this.syncStatus || this.syncBlocker || (this.hasSyncTrack ? '' : 'no sync file for this book')
    },
    userMediaProgress() {
      if (!this.libraryItemId) return
      return this.$store.getters['user/getUserMediaProgress'](this.libraryItemId)
    },
    savedEbookLocation() {
      if (!this.keepProgress) return null
      const location = this.userMediaProgress?.ebookLocation
      return String(location || '').startsWith('epubcfi') ? location : null
    },
    ebookUrl() {
      const base = `/api/items/${this.libraryItemId}/ebook`
      return this.fileId ? `${base}/${this.fileId}` : base
    },
    streamLibraryItem() {
      return this.$store.state.streamLibraryItem
    },
    isPlayingThisItem() {
      return !!this.streamLibraryItem && this.streamLibraryItem.id === this.libraryItemId
    },
    themeStyles() {
      const { theme, font, fontScale, lineSpacing } = this.ereaderSettings
      const isDark = theme === 'dark'
      const isSepia = theme === 'sepia'
      const fontColor = isDark ? '#fff' : isSepia ? '#5b4636' : '#000'
      const backgroundColor = isDark ? 'rgb(35 35 35)' : isSepia ? 'rgb(244 236 216)' : 'rgb(255 255 255)'
      const textStroke = this.ereaderSettings.textStroke / 100
      const family = font === 'sans-serif' ? 'sans-serif' : 'serif'

      // `fontScale` goes on the root only: a percentage font-size on every element compounds per
      // nesting level.
      return `
        html {
          color: ${fontColor} !important;
          background: ${backgroundColor} !important;
          font-size: ${fontScale}% !important;
        }
        html, body, body * {
          line-height: ${lineSpacing}% !important;
        }
        body {
          color: ${fontColor} !important;
          background: ${backgroundColor} !important;
          font-family: ${family} !important;
          -webkit-text-stroke: ${textStroke}px ${fontColor};
          padding: 0 !important;
          margin: 0 !important;
          widows: 0; orphans: 0;
        }
        a { color: ${fontColor} !important; }
        p, li, blockquote { color: ${fontColor} !important; }
        img, svg { max-width: 100% !important; max-height: 100% !important; }
        ::selection { background: rgba(120, 170, 255, .45); }
      `
    }
  },
  watch: {
    playerOpen() {
      this.resize()
    },
    // Reader.vue shows this in its settings; a ref read there would never update.
    syncStatusText: {
      immediate: true,
      handler(text) {
        this.$emit('sync-status', text)
      }
    }
  },
  methods: {
    async initEpub() {
      this.loading = true
      this.loadingMessage = 'Loading ebook…'
      this.windowWidth = window.innerWidth

      try {
        // view.js registers the <foliate-view> custom element as a side effect of loading.
        const [, anchors] = await Promise.all([runtimeImport(FOLIATE_VIEW), runtimeImport(FOLIATE_ANCHORS)])
        this.anchors = anchors

        this.loadingMessage = 'Unpacking ebook…'
        const buffer = await this.$axios.$get(this.ebookUrl, { responseType: 'arraybuffer' })
        const { makeZipLoader } = await runtimeImport(FOLIATE_ZIP)
        const loader = await makeZipLoader(new Uint8Array(buffer))
        const { EPUB } = await runtimeImport(FOLIATE_EPUB)
        this.book = await new EPUB(loader).init()

        // Component was destroyed while the book was downloading.
        if (!this.$refs.frame) return

        this.loadingMessage = 'Rendering…'
        const view = document.createElement('foliate-view')
        Object.assign(view.style, { display: 'block', width: '100%', height: '100%' })
        this.$refs.frame.appendChild(view)
        this.view = view

        // A field so destroy() can remove it.
        this.onRelocateListener = (event) => this.onRelocate(event.detail)
        view.addEventListener('relocate', this.onRelocateListener)
        view.addEventListener('load', this.onSectionLoad)
        view.addEventListener('create-overlay', this.onOverlayCreated)
        // Reader.vue listens for these for swipe gestures.
        view.addEventListener('touchstart', (e) => this.$emit('touchstart', e))
        view.addEventListener('touchend', (e) => this.$emit('touchend', e))

        await view.open(this.book)
        this.applyStyles()

        // Restore the saved position before following starts, so stale progress does not win.
        const location = this.savedEbookLocation
        if (location) {
          await view.goTo(location).catch(() => view.init({ showTextStart: true }))
        } else {
          await view.init({ showTextStart: true })
        }

        this.buildChapters()
        this.loading = false
        this.loadSyncTrack()
      } catch (error) {
        console.error('[EpubReader] failed to open epub', error)
        this.loadingMessage = `Could not open this ebook: ${error.message}`
        this.loading = false
      }
    },

    destroy() {
      this.detachSync()
      if (this.view) {
        this.view.removeEventListener('relocate', this.onRelocateListener)
        this.view.removeEventListener('load', this.onSectionLoad)
        this.view.removeEventListener('create-overlay', this.onOverlayCreated)
        try {
          this.view.close()
        } catch (error) {
          console.warn('[EpubReader] view.close failed', error)
        }
        this.view.remove()
        this.view = null
      }
      this.book = null
    },

    onRelocate(location) {
      if (!location?.cfi || location.cfi === this.currentLocationCfi) return
      this.currentLocationCfi = location.cfi
      // Don't write back the position that was just restored.
      if (this.savedEbookLocation === location.cfi) return

      const payload = { ebookLocation: location.cfi }
      if (location.fraction != null) payload.ebookProgress = location.fraction
      this.updateProgress(payload)
    },

    /** A page turn destroys the old Overlayer. Repaint only: turning here would undo the reader's own navigation. */
    onSectionLoad(event) {
      const doc = event?.detail?.doc
      if (doc && !clickWired.has(doc)) {
        clickWired.add(doc)
        doc.addEventListener('click', (e) => this.onContentClick(e, doc))
      }
      this.applyStyles()
      // A `load` fired by our own renderer.goTo is already on the right page.
      if (this.syncNavigating) return
      this.refreshSyncHighlight(false)
    },

    /** The Overlayer is created before layout, so its rects are empty until redrawn. */
    onOverlayCreated() {
      if (this.syncEnabled) this.refreshSyncHighlight(false)
    },

    applyStyles() {
      const renderer = this.view?.renderer
      if (!renderer?.setStyles) return
      renderer.setStyles(this.themeStyles)
      this.applyLayout()
    },

    /**
     * Set as attributes, since foliate copies them into a closed shadow root that CSS custom
     * properties on the host do not reach. Only written on change: each write re-renders the section.
     */
    applyLayout() {
      this.setRendererAttribute('max-column-count', this.ereaderSettings.spread === 'none' ? '1' : '2')
      this.setRendererAttribute('max-inline-size', `${this.textColumnWidth()}px`)
      if (this.ereaderSettings.flow === 'scrolled') this.setRendererAttribute('flow', 'scrolled')
      else this.view?.renderer?.removeAttribute('flow')
    },

    setRendererAttribute(name, value) {
      const renderer = this.view?.renderer
      if (renderer && renderer.getAttribute(name) !== value) renderer.setAttribute(name, value)
    },

    /** foliate fixes the text column at 720px; this lets it grow with the window. */
    textColumnWidth() {
      const available = this.view?.clientWidth || this.windowWidth || 0
      if (!available) return 720
      return Math.min(Math.max(Math.round(available * 0.62), 560), 1080)
    },

    /** Re-flow invalidates painted rects. Repaints in place and never moves the reader. */
    restyle() {
      this.applyStyles()
      this.redrawHighlight()
      this.refreshSyncHighlight(false)
    },

    prev() {
      return this.view?.prev()
    },
    next() {
      return this.view?.next()
    },
    goToChapter(href) {
      return this.view?.goTo(href)
    },

    /** `sync` rides in the same object as the theme settings; `undefined` counts as on. */
    updateSettings(settings) {
      this.ereaderSettings = { ...this.ereaderSettings, ...settings }
      this.applySyncPreference()
      this.restyle()
    },

    /** Keyed off the timer, not `syncEnabled`: settings arrive before the sidecar has loaded. */
    applySyncPreference() {
      const wanted = this.ereaderSettings.sync !== false && !!this.syncIndex
      const running = this.syncTimer !== null

      if (wanted && !running) {
        this.syncEnabled = true
        this.attachSync()
        // Land on where the audio is now, ignoring the turn rate limit.
        this.refreshSyncHighlight(true, true)
      } else if (!wanted && (running || this.syncEnabled)) {
        this.syncEnabled = false
        this.detachSync()
        this.clearHighlight()
      }
    },

    buildChapters() {
      const flat = []
      const walk = (items) => {
        for (const item of items || []) {
          flat.push({ label: (item.label || '').trim(), href: item.href || '', subitems: item.subitems || [] })
          walk(item.subitems)
        }
      }
      walk(this.book?.toc)

      this.chapters = flat.map((item, index) => ({
        id: index,
        title: item.label || `Section ${index + 1}`,
        href: item.href,
        subitems: item.subitems.map((sub, subIndex) => ({
          id: `${index}-${subIndex}`,
          title: (sub.label || '').trim(),
          href: sub.href,
          subitems: [],
          searchResults: []
        })),
        searchResults: []
      }))
    },

    /** `view.search` isn't vendored, so this walks the sections and attaches `searchResults` to chapters. */
    async searchBook(query) {
      const needle = String(query || '').trim().toLowerCase()
      if (needle.length < 2 || !this.book) return []

      const withResults = (item) => ({ ...item, searchResults: [] })
      const chapters = this.chapters.map((c) => ({ ...withResults(c), subitems: c.subitems.map(withResults) }))
      const byHref = new Map()
      for (const chapter of chapters) {
        byHref.set(this.normaliseHref(chapter.href), chapter)
        for (const sub of chapter.subitems) byHref.set(this.normaliseHref(sub.href), sub)
      }

      for (const [index, section] of this.book.sections.entries()) {
        let doc
        try {
          doc = await section.createDocument()
        } catch (error) {
          console.warn('[EpubReader] search could not load section', index, error)
          continue
        }

        const text = doc.body?.textContent || ''
        const haystack = text.toLowerCase()
        const items = []
        for (
          let at = haystack.indexOf(needle);
          at !== -1 && items.length < MAX_SEARCH_RESULTS_PER_SECTION;
          at = haystack.indexOf(needle, at + needle.length)
        ) {
          const range = this.rangeOfOffsets(doc, at, at + needle.length)
          items.push({
            cfi: this.view.getCFI(index, range),
            excerpt: text.slice(Math.max(0, at - 40), at + needle.length + 60).trim()
          })
        }
        if (!items.length) continue

        // Sections outside the TOC go to the first chapter.
        const target = byHref.get(this.normaliseHref(section.id)) || chapters[0]
        if (target) target.searchResults = items
      }

      return chapters.filter((c) => c.searchResults.length || c.subitems.some((s) => s.searchResults.length))
    },

    rangeOfOffsets(doc, start, end) {
      const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT)
      const range = doc.createRange()
      let seen = 0
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const length = node.data.length
        if (start >= seen && start < seen + length) range.setStart(node, start - seen)
        if (end >= seen && end <= seen + length) {
          range.setEnd(node, end - seen)
          break
        }
        seen += length
      }
      return range
    },

    /** Strip fragment, `./`, and a leading `something.epub/` so sidecar paths match foliate's section ids. */
    normaliseHref(href) {
      let path = String(href || '').split('#')[0]
      try {
        path = decodeURIComponent(path)
      } catch {
        // malformed escape: use the raw path
      }
      path = path.replace(/^\.\//, '')
      const segments = path.split('/')
      if (segments.length > 1 && /\.epub$/i.test(segments[0])) path = segments.slice(1).join('/')
      return path.replace(/^\/+/, '')
    },

    updateProgress(payload) {
      if (!this.keepProgress) return
      this.$axios.$patch(`/api/me/progress/${this.libraryItemId}`, payload, { progress: false }).catch((error) => {
        console.error('[EpubReader] updateProgress failed', error)
      })
    },

    /** Debounced. The player opening or closing resizes the reader without a window resize event. */
    resize() {
      this.windowWidth = window.innerWidth
      window.clearTimeout(this.resizeTimer)
      this.resizeTimer = window.setTimeout(() => {
        this.resizeTimer = null
        this.restyle()
      }, 250)
    },

    async loadSyncTrack() {
      if (!this.smilFile) return
      try {
        const text = await this.$axios.$get(`/api/items/${this.libraryItemId}/file/${this.smilFile.ino}/download`, {
          responseType: 'text',
          transformResponse: [(data) => data]
        })
        // The component may have been closed while the sidecar downloaded.
        if (this.gone) return
        const { cues } = this.anchors.parseSmil(text)
        if (!cues.length) {
          this.syncStatus = 'Sync file is empty'
          return
        }
        // Cue times are positions in one audio file, which only equal playback positions when the book is one file.
        const audioFiles = this.libraryItem?.media?.audioFiles?.length || 0
        if (audioFiles > 1) {
          this.syncStatus = `Sync file covers one audio file, this book has ${audioFiles}`
          return
        }
        this.syncCues = cues
        this.syncIndex = this.anchors.buildCueIndex(cues)
        this.blockCues = this.anchors.indexCuesByBlock(cues, this.normaliseHref)
        this.applySyncPreference()
      } catch (error) {
        console.error('[EpubReader] could not load sync file', error)
        this.syncStatus = 'Sync file could not be read'
      }
    },

    attachSync() {
      this.detachSync()
      this.syncTimer = window.setInterval(this.pollPlaybackClock, SYNC_POLL_MS)
    },

    detachSync() {
      window.clearInterval(this.syncTimer)
      this.syncTimer = null
    },

    playerContainer() {
      return this.$root?.$refs?.mediaPlayerContainer || null
    },

    playbackElement() {
      return document.getElementById('audio-player')
    },

    /**
     * Position within the whole audiobook, or null. The <audio> element is only a fallback: tracks
     * reuse it, so its `currentTime` restarts on every track while the sidecar spans the book.
     */
    playbackPosition() {
      const container = this.playerContainer()
      if (container && Number.isFinite(container.currentTime) && container.currentTime > 0) {
        return container.currentTime
      }
      const el = this.playbackElement()
      if (el && el.readyState > 0 && el.currentTime > 0) return el.currentTime
      return null
    },

    playbackIsRunning() {
      if (this.playerContainer()?.playerIsPlaying) return true
      const el = this.playbackElement()
      return !!el && !el.paused && !el.ended
    },

    pollPlaybackClock() {
      if (!this.syncEnabled) return
      const time = this.playbackPosition()
      this.syncBlocker = !this.syncIndex
        ? 'no sync file'
        : !this.isPlayingThisItem
          ? 'not playing this book'
          : !this.playbackIsRunning()
            ? 'paused'
            : time === null
              ? 'no playback position'
              : ''
      if (!this.syncBlocker) this.onPlayerTime(time)
    },

    onPlayerTime(time) {
      if (!this.syncEnabled || !this.syncIndex) return
      if (!Number.isFinite(time) || time < 0) return

      // A big jump is a seek; it must bypass the turn rate limit, which would crawl towards the target.
      const previous = this.lastSyncTime
      this.lastSyncTime = time
      const jumped = previous >= 0 && Math.abs(time - previous) > SEEK_JUMP_SECONDS

      const cue = this.syncIndex.at(time)
      if (!cue) {
        if (this.syncPaintedCue !== -1) {
          this.clearHighlight()
          this.syncPaintedCue = -1
          this.syncPaintedWord = -1
        }
        return
      }
      const word = this.anchors.wordAt(this.prepareCue(cue), time)

      // Compared with what is painted, not requested: a deferred page turn would look finished.
      if (cue.index === this.syncPaintedCue && word === this.syncPaintedWord) return

      this.syncCueIndex = cue.index
      this.syncWordIndex = word
      // The only caller allowed to turn the page.
      this.refreshSyncHighlight(true, jumped)
    },

    /** A click on a word moves the narration there; selecting text or following a link does not. */
    onContentClick(event, doc) {
      if (this.ereaderSettings.clickToSeek === false || event.button !== 0) return
      if (event.target?.closest?.('a[href]')) return
      if (doc.getSelection()?.toString().trim()) return
      this.seekToPoint(doc, event.clientX, event.clientY)
    },

    seekToPoint(doc, x, y) {
      // Only an existing player session can be moved; nothing is started from here.
      if (!this.syncIndex || !this.blockCues || !this.isPlayingThisItem) return
      const hit = this.anchors.wordAtPoint(doc, x, y)
      const entry = hit && this.renderedContents().find((c) => c.doc === doc)
      if (!entry) return
      const href = this.normaliseHref(this.book?.sections?.[entry.index]?.id)
      const time = this.anchors.seekTimeForWord(this.blockCues.get(`${href}#${hit.block.anchor}`), hit.index)
      // A word the aligner skipped has no time to go to.
      if (time === null) return

      // Through the event bus: `$root.$refs` does not reach the player container.
      this.$eventBus.$emit('seek-playback', time)
      // The poll does not run while paused, so repaint the highlight here.
      this.onPlayerTime(time)
    },

    /** Only possible while the cue's section is the one rendered. */
    prepareCue(cue) {
      if (cue.wordOffsets) return cue
      const entry = this.currentSection()
      if (!entry || this.normaliseHref(entry.href) !== this.normaliseHref(cue.href)) return cue
      const block = this.anchors.findSyncBlock(entry.doc, cue.anchor)
      if (block) this.anchors.attachCueText(cue, block.element)
      return cue
    },

    renderedContents() {
      return this.view?.renderer?.getContents?.() || []
    },

    currentSection() {
      const entry = this.renderedContents().find((c) => c.doc)
      if (!entry) return null
      return {
        index: entry.index,
        doc: entry.doc,
        overlayer: entry.overlayer || null,
        href: this.book?.sections?.[entry.index]?.id || ''
      }
    },

    /**
     * Single-flight, since the poll does not await and a revealRange that finds `syncNavigating`
     * set by another run gives up. `allowTurn` is for audio-driven calls; `urgent` skips the
     * section-turn rate limit.
     */
    async refreshSyncHighlight(allowTurn, urgent = false) {
      if (this.syncTurning) return
      this.syncTurning = true
      try {
        await this.applyHighlight(allowTurn, urgent)
      } finally {
        this.syncTurning = false
      }
    },

    async applyHighlight(allowTurn, urgent) {
      const cue = this.syncEnabled ? this.syncCues?.[this.syncCueIndex] : null
      if (!cue) {
        this.clearHighlight()
        return
      }

      let entry = this.currentSection()
      if (!entry) return

      if (this.normaliseHref(entry.href) !== this.normaliseHref(cue.href)) {
        // Also runs on every section load, where turning would override the reader's navigation.
        if (!allowTurn) {
          this.clearHighlight()
          return
        }
        const now = Date.now()
        if (!urgent && now - this.syncLastJump < SECTION_TURN_GAP_MS) return
        this.syncLastJump = now
        this.clearHighlight()
        try {
          await this.view.goTo(cue.href)
        } catch (error) {
          console.warn('[EpubReader] could not turn to', cue.href, error)
          return
        }
        entry = this.currentSection()
        if (!entry) return
      }

      const block = this.anchors.findSyncBlock(entry.doc, cue.anchor)
      if (!block) {
        // The sidecar and the book disagree about the text.
        this.syncStatus = `No anchor ${cue.anchor} in ${cue.href}`
        return
      }
      this.syncStatus = ''
      this.anchors.attachCueText(cue, block.element)
      this.clearHighlight()

      const { overlayer } = entry
      const words = cue.wordOffsets
      const first = words?.[0]
      const last = words?.[words.length - 1]

      let sentenceRange = null
      if (first && last && last.end > first.start) {
        sentenceRange = this.anchors.rangeForOffsets(block.element, first.start, last.end)
        overlayer?.add(SENTENCE_KEY, sentenceRange, drawHighlight, { color: SENTENCE_COLOR, opacity: SENTENCE_OPACITY })
      }

      const offsets = words?.[this.syncWordIndex]
      if (offsets && offsets.end > offsets.start) {
        const range = this.anchors.rangeForOffsets(block.element, offsets.start, offsets.end)
        if (range) overlayer?.add(WORD_KEY, range, drawUnderline, { color: WORD_COLOR, width: WORD_RULE_WIDTH })
      }

      // A section spans many pages, so paginate to the sentence.
      if (sentenceRange && allowTurn && !(await this.revealRange(entry, sentenceRange))) {
        // Not on the page yet. Leave the painted cue untouched so the next poll retries.
        return
      }
      this.syncPaintedCue = cue.index
      this.syncPaintedWord = this.syncWordIndex
    },

    /**
     * Asks foliate rather than measuring: the iframe holds the whole section in columns, so
     * everything is "visible" against its own width. `lastLocation.range` is the real page.
     */
    rangeOnScreen(range) {
      const visible = this.view?.lastLocation?.range
      if (!range || !visible) return false
      try {
        // Ranges overlap when each starts before the other ends.
        return (
          visible.compareBoundaryPoints(Range.END_TO_START, range) < 0 &&
          range.compareBoundaryPoints(Range.END_TO_START, visible) < 0
        )
      } catch {
        return false // ranges from different documents
      }
    },

    /**
     * Paginate to a range if it is not visible. Resolves true once it is on screen; false means
     * unknown, retry. Guarded against re-entry, since the renderer dispatches `load` while moving.
     */
    async revealRange(entry, range) {
      if (this.syncNavigating) return false
      if (this.rangeOnScreen(range)) return true

      this.syncNavigating = true
      try {
        await this.view.renderer.goTo({ index: entry.index, anchor: range })
        // goTo is a no-op mid-transition and lastLocation only updates once it settles, so wait.
        for (let attempt = 0; attempt < 8; attempt++) {
          await new Promise((resolve) => setTimeout(resolve, 50))
          if (this.rangeOnScreen(range)) return true
        }
        return false
      } catch (error) {
        console.warn('[EpubReader] could not reveal range', error)
        return false
      } finally {
        this.syncNavigating = false
      }
    },

    clearHighlight() {
      for (const { overlayer } of this.renderedContents()) {
        overlayer?.remove(SENTENCE_KEY)
        overlayer?.remove(WORD_KEY)
      }
    },

    /** Overlayer caches rects, so recompute them after a re-flow. */
    redrawHighlight() {
      for (const { overlayer } of this.renderedContents()) {
        try {
          overlayer?.redraw()
        } catch {
          // overlay is mid-teardown; the next load repaints it
        }
      }
    }
  },
  mounted() {
    window.addEventListener('resize', this.resize)
    this.initEpub()
  },
  beforeDestroy() {
    this.gone = true
    window.removeEventListener('resize', this.resize)
    window.clearTimeout(this.resizeTimer)
    this.destroy()
  }
}
</script>
