# MIGRATION_MAP — Video Player Phase 1

## PLAYER_CODE (sẽ xóa)

### Functions trong VP controller (app.js lines 645–1174)
- `VP.init()` — khởi tạo, gắn event listeners
- `VP.load(name, url, subs, isHls, hlsMasterUrl)` — load video source
- `VP._loadHls(masterUrl)` — init HLS.js player
- `VP._setupQuality(levels)` — quality selector menu
- `VP._onLevelSwitch(level)` — auto quality badge update
- `VP._setupSubs(subs)` — subtitle track menu
- `VP.togglePlay()` — play/pause
- `VP.toggleMute()` — mute/unmute
- `VP.toggleFs()` — fullscreen
- `VP._onPlayState()` — update play/pause UI
- `VP._onEnded()` — video ended handler
- `VP._getDuration()` — get duration (with fallback)
- `VP._onTimeUpdate()` — update progress bar + time display + auto-save position
- `VP._onMeta()` — loadedmetadata handler
- `VP._setKnownDuration(seconds)` — set duration from probe data
- `VP._pollRemux(path)` / `VP.stopRemuxPoll()` — remux polling
- `VP._onBuffer()` — buffered range display
- `VP._startSeek(e)` / `VP._hoverSeek(e)` — seek bar interaction
- `VP._updateVolIcon()` — volume icon state
- `VP._showControls()` / `VP._scheduleHide()` — auto-hide controls
- `VP._onFsChange()` — fullscreen change handler
- `VP._savePosition()` — save playback position to localStorage
- `VP._fmtTime(s)` — format seconds to "h:mm:ss" or "m:ss"

### Biến global
- `hlsPlayer` (line 642) — HLS.js instance
- `VP` object (lines 645–1174) — entire controller

### Event listeners trên `<video>`
- `play`, `pause`, `ended`, `timeupdate`, `loadedmetadata`, `progress`, `waiting`, `playing`, `canplay`
- Keyboard: Space/k, m, f, Arrow keys
- `beforeunload` → `VP._savePosition()` (line 1177)

### HTML (index.html lines 241–303)
- `#playerW` (.vp-container) — player wrapper
- `#vpWrapper` (.vp-wrapper) — video + controls wrapper
- `#playerE` (.player-empty) — placeholder
- `#vp` — video element
- `#vpBuffer` (.vp-buffer) — buffering spinner
- `#vpBigPlay` (.vp-big-play) — big play overlay
- `#vpControls` (.vp-controls) — controls bar
- `#vpProgressWrap` — progress bar + hover time
- `#vpPlayed`, `#vpBuffered`, `#vpScrubber` — progress indicators
- `#vpHoverTime` — hover time tooltip
- `#vpPlayBtn`, `#vpTimeCur`, `#vpTimeDur` — play button + time display
- `#vpMuteBtn`, `#vpVolSlider` — volume controls
- `#vpSpeedBtn`, `#vpSpeedMenu` — speed menu
- `#vpSubWrap`, `#vpSubMenu`, `#vpSubBtn` — subtitle selector
- `#vpQualityWrap`, `#vpQualityMenu`, `#vpQualityBtn` — quality selector
- `#vpFsBtn` — fullscreen button

### CSS (style.css lines 140–201)
- `.vp-container`, `.vp-wrapper`, `.vp-wrapper video`
- `.player-empty`
- `.vp-big-play`, `.vp-buffer`, `.vp-buffer-spin`
- `.vp-controls`, `.vp-progress-*`, `.vp-played`, `.vp-buffered`, `.vp-scrubber`
- `.vp-hover-time`, `.vp-btns-*`, `.vp-btn`, `.vp-time`
- `.vp-vol-*`, `.vp-speed-*`, `.vp-sub-*`, `.vp-quality-*`

---

## GIỮ NGUYÊN (không đụng)

- Tất cả `fetch()`/`api()` calls
- Auth: `auth()`, `jwtLogin()`, `logout()`, `showAuth()`
- Routing: `go()`, `stab()`
- Downloads: `subUrl()`, `subCurl()`, `cancelDl()`, `resumeDl()`, `rAll()`, `renderDL()`
- File Manager: `rFiles()`, `renderFM()`, `fmGo()`, `fmSel()`, `fmUpload()`, ...
- Media library rendering: `rMedia()`, `renderM()`
- Recycle bin, dedup, share links
- Upload Manager (UPM)
- Dialog system: `dlgConfirm()`, `dlgPrompt()`, `dlgAlert()`
- Utils: `toast()`, `hs()`, `esc()`, `cpL()`
- Context menu: `fmCtx()`, `ctxAction()`
- `navigateToFile()` — keeps routing logic
- `startHls(path)` — API call only, no player logic

---

## INTERFACE (điểm kết nối)

### Entry point functions
- `playMediaFile(path)` — main entry, handles probe + remux + stream URL (lines 1180–1282)
- `playM(name, url, subs)` — calls `VP.load()` (line 1283–1285)
- `playHls(name, masterUrl, subs)` — loads HLS.js + calls `VP.load()` (lines 1287–1290)
- `loadHlsLib()` — dynamic script loader for HLS.js (line 1286)

### Path format
- Relative path from `DOWNLOAD_DIR`, e.g.: `'video.mp4'`, `'movies/BigBuck.mkv'`

### Container elements
- `#p-media` — media page container
- `#playerW` (.vp-container) — player wrapper (display:none initially)
- `#nowP` (.now-playing) — now playing bar
- `#mediaList` — media library grid

### Backend endpoints used
- `GET /api/media` — list media files + subtitles + HLS status
- `GET /api/media/probe/{path}` — codec check + duration + remux status
- `POST /api/media/remux/{path}` — trigger background remux
- `GET /api/media/remux-status/{path}` — poll remux progress
- `GET /stream/{path}` — direct file streaming (range requests)
- `GET /stream-transcode/{path}` — on-the-fly ffmpeg transcode
- `POST /api/media/hls/{path}` — trigger HLS transcoding
- `/hls/{hash}/master.m3u8` — HLS master playlist (static mount)
- `/hls/{hash}/{profile}/index.m3u8` — quality-specific playlist
- `/hls/{hash}/{profile}/seg_NNNN.ts` — HLS segments (static mount)
