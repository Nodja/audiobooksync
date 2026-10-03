<template>
  <div id="epub-frame" class="relative w-full h-full">
    <div class="fixed left-0 h-8 w-full px-4 flex items-center" :class="isLightTheme ? 'bg-white text-black' : 'bg-[#232323] text-white/80'" :style="{ bottom: playerHeight + 'px' }">
      <button v-if="hasSyncTrack" type="button" class="flex items-center gap-1 rounded px-1 py-0.5 text-xxs opacity-70 disabled:opacity-40" :disabled="!hasSyncTrack" @click.stop="toggleSync">
        <span class="material-symbols text-sm">{{ syncEnabled ? 'sync' : 'sync_disabled' }}</span>
        <span class="hidden sm:inline">{{ syncEnabled ? $strings.LabelSyncWithAudio : $strings.LabelSyncWithAudioOff }}</span>
      </button>
      <div class="flex-grow" />
      <p v-if="syncStatus" class="mr-2 text-xxs opacity-60">{{ syncStatus }}</p>
      <p class="text-xs">{{ progress }}%</p>
    </div>

    <div id="viewer" class="h-full w-full"></div>

    <div v-if="loading" class="absolute inset-0 flex items-center justify-center" :class="isLightTheme ? 'bg-white' : 'bg-[#232323]'">
      <p class="text-sm opacity-80">{{ loadingMessage }}</p>
    </div>
  </div>
</template>

<script>
import { AbsAudioPlayer } from '@/plugins/capacitor'
import { sidecarCache } from '@/plugins/sidecarCache'

// foliate-js is imported at runtime rather than bundled: webpack 4 has no `webpackIgnore` and would
// try to resolve these paths at build time. `static/` is served from the app's own origin.
const FOLIATE_VIEW = '/js/foliate/view.js'
const FOLIATE_ANCHORS = '/js/foliate/abs-anchors.js'
const FOLIATE_ZIP = '/js/foliate/vendor/zip-loader.js'
const FOLIATE_EPUB = '/js/foliate/epub.js'

/** `new Function` hides the `import()` from webpack while leaving a real dynamic import. */
const runtimeImport = new Function('url', 'return import(url)')

const isSmil = (file) => /\.smil$/i.test(file?.metadata?.filename || '')

/** Each sample crosses the native bridge, so this is slower than a DOM `timeupdate`. */
const SYNC_POLL_MS = 120

/** Minimum gap between section turns, so scrubbing cannot thrash the renderer. */
const MAX_JUMP_MS = 1200

/** A clock jump bigger than this between two polls is a seek, not playback. */
const SEEK_JUMP_SECONDS = 5

/** Two taps within this long, and this close (px), are a double-tap; a tap moves less than TAP_SLOP and ends within TAP_MS. */
const DOUBLE_TAP_MS = 350
const DOUBLE_TAP_DISTANCE = 40
const TAP_SLOP = 12
const TAP_MS = 300

// Overlayer keys: one painted layer per key, so re-adding replaces rather than stacks.
const SENTENCE_KEY = 'abs-sync-sentence'
const WORD_KEY = 'abs-sync-word'
const SENTENCE_COLOR = '#5b8def'
const SENTENCE_OPACITY = 0.28
const WORD_COLOR = '#f0a020'
const WORD_RULE_WIDTH = 2

const SVG_NS = 'http://www.w3.org/2000/svg'

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
    url: String,
    libraryItem: {
      type: Object,
      default: () => {}
    },
    isLocal: Boolean,
    keepProgress: Boolean
  },
  data() {
    return {
      book: null,
      view: null,
      anchors: null,
      loading: true,
      loadingMessage: 'Loading ebook…',
      progress: 0,
      currentLocationCfi: null,
      inittingDisplay: true,
      isRefreshingUI: false,
      playerHeight: 0,
      syncCues: null,
      syncIndex: null,
      blockCues: null,
      tapStart: null,
      lastTap: null,
      syncEnabled: false,
      syncStatus: '',
      syncCueIndex: -1,
      syncWordIndex: -1,
      // A deferred page turn leaves these behind the requested cue, which is what makes the poll retry it.
      syncPaintedCue: -1,
      syncPaintedWord: -1,
      lastSyncTime: -1,
      syncLastJump: 0,
      syncTimer: null,
      syncPolling: false,
      // The renderer dispatches `load` while moving, which must not re-enter.
      syncNavigating: false,
      syncTurning: false,
      ereaderSettings: {
        theme: 'dark',
        font: 'serif',
        fontScale: 100,
        lineSpacing: 115,
        spread: 'auto',
        textStroke: 0,
        clickToSeek: true
      }
    }
  },
  computed: {
    libraryItemId() {
      return this.libraryItem?.id
    },
    localLibraryItem() {
      if (this.isLocal) return this.libraryItem
      return this.libraryItem.localLibraryItem || null
    },
    localLibraryItemId() {
      return this.localLibraryItem?.id
    },
    serverLibraryItemId() {
      if (!this.isLocal) return this.libraryItem.id
      if (!this.libraryItem.serverAddress || !this.libraryItem.libraryItemId) return null
      if (this.$store.getters['user/getServerAddress'] === this.libraryItem.serverAddress) {
        return this.libraryItem.libraryItemId
      }
      return null
    },
    isPlayerOpen() {
      return this.$store.getters['getIsPlayerOpen']
    },
    /** The fixed `h-8` bar overlaps the book, so the book is shortened by the bar plus the player. */
    readerHeightOffset() {
      return this.playerHeight + 32
    },
    /** foliate's `book.toc` has no `id`, which `Reader.vue` needs for `v-for :key`. */
    chapters() {
      const seen = new Map()
      return (this.book?.toc || []).map((item, index) => {
        const count = (seen.get(item.href) || 0) + 1
        seen.set(item.href, count)
        return {
          id: `${index}-${count}`,
          label: (item.label || '').trim(),
          href: item.href || '',
          subitems: (item.subitems || []).map((sub, subIndex) => ({
            id: `${index}-${count}-${subIndex}`,
            label: (sub.label || '').trim(),
            href: sub.href || '',
            subitems: []
          }))
        }
      })
    },
    userItemProgress() {
      if (this.isLocal) return this.localItemProgress
      return this.serverItemProgress
    },
    localItemProgress() {
      return this.$store.getters['globals/getLocalMediaProgressById'](this.localLibraryItemId)
    },
    serverItemProgress() {
      return this.$store.getters['user/getUserMediaProgress'](this.serverLibraryItemId)
    },
    savedEbookLocation() {
      if (!this.keepProgress) return null
      if (!this.userItemProgress?.ebookLocation) return null
      if (!String(this.userItemProgress.ebookLocation).startsWith('epubcfi')) return null
      return this.userItemProgress.ebookLocation
    },
    isLightTheme() {
      return this.ereaderSettings.theme === 'light'
    },
    hasSyncTrack() {
      return !!this.syncCues?.length
    },
    /** Which server item the offline sidecar cache files this book under. */
    sidecarKey() {
      return this.serverLibraryItemId || this.localLibraryItem?.libraryItemId || null
    },
    isPlayingThisItem() {
      if (!this.serverLibraryItemId) return false
      return this.$store.getters['getIsMediaStreaming'](this.serverLibraryItemId)
    },
    /** A `.smil` sidecar shows up here after a scan. Local items have none. */
    libraryFiles() {
      if (this.isLocal) return []
      return this.libraryItem?.libraryFiles || []
    },
    themeStyles() {
      const theme = this.ereaderSettings.theme
      const isBlack = theme === 'black'
      const isSepia = theme === 'sepia'
      const fontColor = theme === 'dark' || isBlack ? '#fff' : isSepia ? '#5b4636' : '#000'
      const backgroundColor =
        theme === 'dark' ? 'rgb(35 35 35)' : isBlack ? 'rgb(0 0 0)' : isSepia ? 'rgb(244, 236, 216)' : 'rgb(255, 255, 255)'
      const lineSpacing = this.ereaderSettings.lineSpacing
      const fontScale = this.ereaderSettings.fontScale
      const textStroke = this.ereaderSettings.textStroke / 100
      const family = this.ereaderSettings.font === 'sans-serif' ? 'sans-serif' : 'serif'

      // `fontScale` is on the root only: a percentage font-size on every element compounds per nesting level.
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
          padding: 0 !important; margin: 0 !important;
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
    isPlayerOpen() {
      // The player renders from the same state change, so wait for it before measuring.
      this.$nextTick(() => this.refreshUI())
    }
  },
  methods: {
    /** Server items go through axios for its token refresh; local files are served from a scheme axios is not pointed at. */
    loadBytes() {
      if (!this.isLocal) {
        return this.$axios.$get(this.url, { responseType: 'arraybuffer' })
      }
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest()
        xhr.open('GET', this.url, true)
        xhr.responseType = 'arraybuffer'
        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) resolve(xhr.response)
          else reject(new Error(`HTTP ${xhr.status}`))
        }
        xhr.onerror = () => reject(new Error('network error'))
        xhr.send()
      })
    },

    async initEpub() {
      this.loading = true
      this.progress = Math.round((this.userItemProgress?.ebookProgress || 0) * 100)

      try {
        // Importing `view.js` registers the `foliate-view` custom element.
        const [, anchorsModule] = await Promise.all([runtimeImport(FOLIATE_VIEW), runtimeImport(FOLIATE_ANCHORS)])
        this.anchors = anchorsModule

        this.loadingMessage = 'Unpacking ebook…'
        const buffer = await this.loadBytes()
        const { makeZipLoader } = await runtimeImport(FOLIATE_ZIP)
        const loader = await makeZipLoader(new Uint8Array(buffer))

        const { EPUB } = await runtimeImport(FOLIATE_EPUB)
        this.book = await new EPUB(loader).init()

        this.loadingMessage = 'Rendering…'
        const view = document.createElement('foliate-view')
        view.style.display = 'block'
        view.style.width = '100%'
        view.style.height = `calc(100% - ${this.readerHeightOffset}px)`
        document.getElementById('viewer').appendChild(view)
        this.view = view

        this.onRelocateListener = (event) => this.onRelocate(event.detail)
        view.addEventListener('relocate', this.onRelocateListener)
        view.addEventListener('load', this.onSectionLoad)
        view.addEventListener('create-overlay', this.onOverlayCreated)

        await view.open(this.book)
        this.applyStyles()

        const location = this.savedEbookLocation
        if (location) {
          await view.goTo(location).catch(() => view.init({ showTextStart: true }))
        } else {
          await view.init({ showTextStart: true })
        }
        this.inittingDisplay = false
        this.currentLocationCfi = location || null

        // The `isPlayerOpen` watcher never fires if playback was already running when the book opened.
        this.refreshUI()
        this.loading = false
        this.$emit('loaded')
        this.$emit('hook:mounted')
        this.loadSyncTrack()
      } catch (error) {
        console.error('[EpubReader] failed to open epub', error)
        this.loadingMessage = `Could not open this ebook: ${error.message}`
        this.loading = false
      }
    },

    onRelocate(location) {
      if (!location || this.inittingDisplay) return
      if (location.cfi && this.currentLocationCfi === location.cfi) return
      this.currentLocationCfi = location.cfi
      const fraction = location.fraction ?? location.pageItem?.fraction ?? null
      if (fraction === null) {
        this.updateProgress({ ebookLocation: location.cfi })
      } else {
        this.progress = Math.round(fraction * 100)
        this.updateProgress({ ebookLocation: location.cfi, ebookProgress: fraction })
      }
    },

    /** Touches inside the section iframe do not bubble to `document.body`, where `Reader.vue` listens. */
    forwardTouch(event) {
      this.$emit(event.type, event)
    },

    /** A page turn discards the old overlay. Repaint only: turning here would undo the reader's own navigation. */
    onSectionLoad({ detail: { doc } }) {
      doc.addEventListener('touchstart', this.forwardTouch)
      doc.addEventListener('touchend', this.forwardTouch)
      doc.addEventListener('touchstart', this.onContentTouchStart, { passive: true })
      doc.addEventListener('touchend', this.onContentTouchEnd)
      this.applyStyles()
      if (this.syncNavigating) return
      this.refreshSyncHighlight(false)
    },

    onOverlayCreated() {
      if (!this.syncEnabled) return
      this.refreshSyncHighlight(false)
    },

    applyStyles() {
      const renderer = this.view?.renderer
      if (!renderer || !renderer.setStyles) return
      renderer.setStyles(this.themeStyles)
      this.applyLayout()
    },

    /** Written only on change: setting an attribute re-renders the whole section. */
    applyLayout() {
      this.setRendererAttribute('max-column-count', this.ereaderSettings.spread === 'none' ? '1' : '2')
      this.setRendererAttribute('max-inline-size', `${this.textColumnWidth()}px`)
    },

    setRendererAttribute(name, value) {
      const renderer = this.view?.renderer
      if (renderer && renderer.getAttribute(name) !== value) renderer.setAttribute(name, value)
    },

    /** foliate fixes the text column at 720px, which is too narrow on a tablet. */
    textColumnWidth() {
      const available = this.view?.clientWidth || window.innerWidth || 0
      if (!available) return 720
      return Math.min(Math.max(Math.round(available * 0.62), 420), 900)
    },

    /** Re-apply the theme and re-lay the highlight after a settings or layout change. */
    restyle() {
      this.applyStyles()
      for (const entry of this.view?.renderer?.getContents?.() || []) entry.overlayer?.redraw()
      this.refreshSyncHighlight(false)
    },

    updateSettings(settings) {
      this.ereaderSettings = settings
      this.restyle()
    },
    goToChapter(href) {
      if (!this.view) return
      return this.view.goTo(href)
    },
    next() {
      if (!this.view) return
      return this.view.next()
    },
    prev() {
      if (!this.view) return
      return this.view.prev()
    },

    async updateProgress(payload) {
      if (!this.keepProgress) return

      if (this.localLibraryItemId) {
        const localResponse = await this.$db.updateLocalEbookProgress({
          localLibraryItemId: this.localLibraryItemId,
          ...payload
        })
        if (localResponse.localMediaProgress) {
          this.$store.commit('globals/updateLocalMediaProgress', localResponse.localMediaProgress)
        }
      }

      if (this.serverLibraryItemId) {
        this.$nativeHttp.patch(`/api/me/progress/${this.serverLibraryItemId}`, payload).catch((error) => {
          console.error('EpubReader.updateProgress failed:', error)
        })
      }
    },

    async screenOrientationChange() {
      if (this.isRefreshingUI) return
      this.isRefreshingUI = true
      const windowWidth = window.innerWidth
      this.refreshUI()
      // Window width does not always change right away; iPhone 10 on iOS 16 took 100-200ms.
      for (let i = 0; i < 5; i++) {
        await new Promise((resolve) => setTimeout(resolve, 50))
        if (window.innerWidth !== windowWidth) {
          this.refreshUI()
          break
        }
      }
      this.isRefreshingUI = false
    },

    refreshUI() {
      const view = this.view
      if (!view) return
      this.measurePlayer()
      view.style.height = `calc(100% - ${this.readerHeightOffset}px)`
      this.restyle()
    },

    measurePlayer() {
      const player = document.getElementById('playerContent')
      this.playerHeight = player ? player.offsetHeight : 0
    },

    /** The `.smil` sidecar in the item's library files, if the generator wrote one. */
    findSmilFile() {
      return this.libraryFiles.find(isSmil) || null
    },

    /**
     * The sidecar's current file entry on the server: null when the server says there is none,
     * undefined when it could not be asked.
     */
    async serverSmilFile() {
      if (!this.serverLibraryItemId) return undefined
      if (!this.isLocal) return this.findSmilFile()
      // A local item carries no library files, so the listing has to come from the server.
      try {
        const item = await this.$nativeHttp.get(`/api/items/${this.serverLibraryItemId}`)
        return (item?.libraryFiles || []).find(isSmil) || null
      } catch (error) {
        console.warn('[EpubReader] could not ask the server about the sync file', error)
        return undefined
      }
    },

    downloadSmil(smil) {
      // The server only sends CORS headers for the ebook and cover routes, so an XHR to this one
      // is blocked by the WebView. The native HTTP plugin has no origin restrictions.
      return this.$nativeHttp.get(`/api/items/${this.serverLibraryItemId}/file/${smil.ino}/download`)
    },

    /**
     * The sidecar text.  A downloaded book keeps a copy for offline use, refreshed when the server's
     * file has changed; a streamed one is fetched each time.
     */
    async fetchSidecarText() {
      const key = this.sidecarKey
      if (!this.localLibraryItem || !key) {
        const smil = this.findSmilFile()
        return smil ? this.downloadSmil(smil) : null
      }

      const smil = await this.serverSmilFile()
      if (smil === null) {
        sidecarCache.remove(key)
        return null
      }
      const remote = smil ? { ino: smil.ino, size: smil.metadata?.size ?? 0, mtimeMs: smil.metadata?.mtimeMs ?? 0 } : null
      return sidecarCache.resolve({ key, remote, fetchText: () => this.downloadSmil(smil) })
    },

    async loadSyncTrack() {
      try {
        const text = await this.fetchSidecarText()
        // The reader may have been closed while the sidecar downloaded.
        if (this.gone || !text) return
        const parsed = this.anchors.parseSmil(text)
        if (!parsed.cues.length) {
          this.syncStatus = 'Sync file is empty'
          return
        }
        // Cue times are positions in one audio file, which only equal playback positions when the book is one file.
        const audioFiles = this.libraryItem?.media?.audioFiles?.length || 0
        if (audioFiles > 1) {
          this.syncStatus = `Sync file covers one audio file, this book has ${audioFiles}`
          return
        }
        this.syncCues = parsed.cues
        this.syncIndex = this.anchors.buildCueIndex(parsed.cues)
        this.blockCues = this.anchors.indexCuesByBlock(parsed.cues, this.normaliseHref)
        this.syncEnabled = true
        this.attachSync()
        this.refreshSyncHighlight(true, true)
      } catch (error) {
        console.error('[EpubReader] could not load sync file', error)
        this.syncStatus = 'Sync file could not be read'
      }
    },

    attachSync() {
      this.detachSync()
      this.syncTimer = setInterval(this.pollPlaybackClock, SYNC_POLL_MS)
    },

    detachSync() {
      if (this.syncTimer) {
        clearInterval(this.syncTimer)
        this.syncTimer = null
      }
    },

    /** Playback is native, so the clock is asked for over the plugin bridge, as `AudioPlayer.vue` does. */
    async pollPlaybackClock() {
      if (this.syncPolling || !this.syncEnabled || !this.isPlayingThisItem) return
      this.syncPolling = true
      try {
        const { value } = await AbsAudioPlayer.getCurrentTime()
        this.onPlayerTime(value)
      } finally {
        this.syncPolling = false
      }
    },

    onPlayerTime(time) {
      if (!this.syncEnabled || !this.syncIndex) return
      if (!Number.isFinite(time) || time < 0) return

      // A seek must bypass the turn rate limiter, which would otherwise crawl towards the target.
      const previous = this.lastSyncTime
      this.lastSyncTime = time
      const jumped = previous >= 0 && Math.abs(time - previous) > SEEK_JUMP_SECONDS

      const cue = this.syncIndex.at(time)
      if (!cue) {
        // Before the first cue there is nothing to follow.
        if (this.syncPaintedCue !== -1) {
          this.clearHighlight()
          this.syncPaintedCue = -1
          this.syncPaintedWord = -1
        }
        return
      }
      const word = this.anchors.wordAt(this.prepareCue(cue), time)
      if (cue.index === this.syncPaintedCue && word === this.syncPaintedWord) return
      this.syncCueIndex = cue.index
      this.syncWordIndex = word
      this.refreshSyncHighlight(true, jumped)
    },

    /** Turning on jumps straight to where the audio is, bypassing the turn rate limit. */
    toggleSync() {
      if (!this.hasSyncTrack) return
      this.syncEnabled = !this.syncEnabled
      if (this.syncEnabled) this.attachSync()
      else {
        this.detachSync()
        this.clearHighlight()
      }
      this.refreshSyncHighlight(true, true)
    },

    onContentTouchStart(event) {
      const touch = event.changedTouches[0]
      this.tapStart = event.touches.length > 1 ? null : { x: touch.clientX, y: touch.clientY, time: Date.now() }
    },

    /** A double-tap on a word moves the narration there. Reader.vue holds back its own tap action to let this through. */
    onContentTouchEnd(event) {
      const touch = event.changedTouches[0]
      const start = this.tapStart
      this.tapStart = null
      const now = Date.now()
      const isTap = start && now - start.time < TAP_MS && Math.hypot(touch.clientX - start.x, touch.clientY - start.y) < TAP_SLOP
      if (!isTap) {
        this.lastTap = null
        return
      }

      const last = this.lastTap
      if (!last || now - last.time > DOUBLE_TAP_MS || Math.hypot(touch.clientX - last.x, touch.clientY - last.y) > DOUBLE_TAP_DISTANCE) {
        this.lastTap = { time: now, x: touch.clientX, y: touch.clientY }
        return
      }
      this.lastTap = null
      if (event.target?.closest?.('a[href]')) return
      this.seekToPoint(event.target.ownerDocument, touch.clientX, touch.clientY)
    },

    seekToPoint(doc, x, y) {
      if (this.ereaderSettings.clickToSeek === false) return
      // Only an existing player session can be moved; nothing is started from here.
      if (!this.syncIndex || !this.blockCues || !this.isPlayingThisItem) return
      const hit = this.anchors.wordAtPoint(doc, x, y)
      const entry = hit && (this.view?.renderer?.getContents?.() || []).find((c) => c.doc === doc)
      if (!entry) return
      const href = this.normaliseHref(this.book?.sections?.[entry.index]?.id)
      const time = this.anchors.seekTimeForWord(this.blockCues.get(`${href}#${hit.block.anchor}`), hit.index)
      // A word the aligner skipped has no time to go to.
      if (time === null) return

      AbsAudioPlayer.seek({ value: time })
      // The poll only runs while following, so repaint the highlight here.
      this.onPlayerTime(time)
    },

    /** Attach character offsets to a cue's words, once its paragraph is on screen. */
    prepareCue(cue) {
      if (cue.wordOffsets) return cue
      const entry = this.currentSection()
      if (!entry || this.normaliseHref(entry.href) !== this.normaliseHref(cue.href)) return cue
      const block = this.anchors.findSyncBlock(entry.doc, cue.anchor)
      if (!block) return cue
      this.anchors.attachCueText(cue, block.element)
      return cue
    },

    /** The section currently rendered, with its href, document and overlay. */
    currentSection() {
      const contents = this.view?.renderer?.getContents?.() || []
      const entry = contents.find((c) => c.doc)
      if (!entry) return null
      return {
        index: entry.index,
        doc: entry.doc,
        overlayer: entry.overlayer || null,
        href: this.book?.sections?.[entry.index]?.id || ''
      }
    },

    /** Single-flight, because a reveal that finds the renderer already moving gives up. */
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
        if (!allowTurn) {
          this.clearHighlight()
          return
        }
        const now = Date.now()
        if (!urgent && now - this.syncLastJump < MAX_JUMP_MS) return
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
        // The sidecar and this book disagree about the text.
        this.syncStatus = `No anchor ${cue.anchor} in ${cue.href}`
        return
      }
      this.syncStatus = ''
      this.anchors.attachCueText(cue, block.element)

      this.clearHighlight()

      const offsets = cue.wordOffsets || []
      const first = offsets[0]
      const last = offsets[offsets.length - 1]
      let sentenceRange = null
      if (first && last && last.end > first.start && entry.overlayer) {
        const range = this.anchors.rangeForOffsets(block.element, first.start, last.end)
        if (range) {
          sentenceRange = range
          entry.overlayer.add(SENTENCE_KEY, range, drawHighlight, {
            color: SENTENCE_COLOR,
            opacity: SENTENCE_OPACITY
          })
        }
      }

      const word = offsets[this.syncWordIndex]
      let wordRange = null
      if (word && word.end > word.start && entry.overlayer) {
        const range = this.anchors.rangeForOffsets(block.element, word.start, word.end)
        if (range) {
          wordRange = range
          entry.overlayer.add(WORD_KEY, range, drawUnderline, {
            color: WORD_COLOR,
            width: WORD_RULE_WIDTH
          })
        }
      }

      // The narrated word is what has to be visible: a sentence can end in the next column, and
      // scrolling to its start would leave the spoken words off screen.
      const target = wordRange || sentenceRange
      if (target && allowTurn && !(await this.revealRange(entry, target))) return

      this.syncPaintedCue = cue.index
      this.syncPaintedWord = this.syncWordIndex
    },

    /**
     * Asks foliate rather than measuring: the paginator's iframe holds the whole section laid out in
     * columns, so everything is "visible" against its own size. `lastLocation.range` is the real page.
     */
    rangeOnScreen(range) {
      const visible = this.view?.lastLocation?.range
      if (!range || !visible) return false
      try {
        return visible.compareBoundaryPoints(Range.END_TO_START, range) < 0 && range.compareBoundaryPoints(Range.END_TO_START, visible) < 0
      } catch {
        // Mid-transition `lastLocation` is still in the previous section's document.
        return false
      }
    },

    /** @returns {Promise<boolean>} true once the range is on screen; false means "unknown, retry". */
    async revealRange(entry, range) {
      if (this.syncNavigating) return false
      if (this.rangeOnScreen(range)) return true
      this.syncNavigating = true
      try {
        await this.view.renderer.goTo({ index: entry.index, anchor: range })
        // goTo is a no-op mid-transition and lastLocation only updates once it settles.
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
      for (const entry of this.view?.renderer?.getContents?.() || []) {
        if (!entry.overlayer) continue
        entry.overlayer.remove(SENTENCE_KEY)
        entry.overlayer.remove(WORD_KEY)
      }
    },

    /** Sidecars may prefix the path with the epub's own filename; foliate names sections by container path. */
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
    }
  },
  mounted() {
    this.initEpub()

    if (screen.orientation) {
      // Not available on ios
      screen.orientation.addEventListener('change', this.screenOrientationChange)
    } else {
      document.addEventListener('orientationchange', this.screenOrientationChange)
    }
    window.addEventListener('resize', this.screenOrientationChange)
  },
  beforeDestroy() {
    this.gone = true
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

    if (screen.orientation) {
      screen.orientation.removeEventListener('change', this.screenOrientationChange)
    } else {
      document.removeEventListener('orientationchange', this.screenOrientationChange)
    }
    window.removeEventListener('resize', this.screenOrientationChange)
  }
}
</script>
