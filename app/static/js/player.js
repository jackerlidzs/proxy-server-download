(function() {
  'use strict';

  let plyrInstance  = null;
  let hlsInstance   = null;
  let currentPath   = null;

  // ─── PUBLIC API ──────────────────────────────────────────
  // app.js gọi hàm này thay cho VP.play() cũ
  // Ví dụ: window.PlayerModule.open('movies/BigBuck.mkv', 'Big Buck Bunny')

  window.PlayerModule = {

    open: async function(filePath, fileName, fallbackStreamUrl) {
      currentPath = filePath;
      destroyPlayer();

      // Show player container
      var pw = document.getElementById('playerW');
      if (pw) pw.style.display = 'block';

      // Cập nhật now-playing bar (giữ logic cũ của #nowP)
      var nowP = document.getElementById('nowP');
      if (nowP) {
        nowP.style.display = 'flex';
        nowP.textContent = '🎬 ' + (fileName || filePath);
      }

      // Fetch subtitles + HLS status in parallel
      var subsPromise = fetchSubtitles(filePath);
      var statusData = await fetchHlsStatus(filePath);
      var subs = await subsPromise;

      // Inject <track> elements before Plyr init
      injectSubtitleTracks(subs);

      if (statusData.status === 'ready') {
        // HLS đã sẵn sàng → dùng luôn (seek instant, nhẹ server)
        loadPlayer(filePath, statusData.master_url, subs);

      } else {
        // HLS chưa có → play NGAY qua URL đã được probe
        var directUrl = fallbackStreamUrl || ('/stream-transcode/' + encodeFilePath(filePath));
        loadPlayerDirect(filePath, directUrl, subs);
      }
    },

    close: function() {
      destroyPlayer();
      currentPath = null;
      var pw = document.getElementById('playerW');
      if (pw) pw.style.display = 'none';
      var nowP = document.getElementById('nowP');
      if (nowP) nowP.style.display = 'none';
    }
  };

  // ─── CORE PLAYER ─────────────────────────────────────────

  async function loadPlayer(filePath, masterUrl, subs) {
    var videoEl = document.getElementById('player');
    if (!videoEl) return;

    if (typeof Hls !== 'undefined' && Hls.isSupported()) {
      hlsInstance = new Hls({
        maxBufferLength: 30,
        maxMaxBufferLength: 60,
      });
      hlsInstance.loadSource(masterUrl);
      hlsInstance.attachMedia(videoEl);
    } else if (videoEl.canPlayType('application/vnd.apple.mpegurl')) {
      // Safari native HLS
      videoEl.src = masterUrl;
    }

    // Build controls: YouTube/Netflix style layout
    var controls = [
      'play-large',
      'play',
      'progress',
      'current-time',
      'mute',
      'volume',
      'captions',
      'settings',
      'pip',
      'fullscreen'
    ];
    var settings = ['speed', 'quality'];
    if (subs && subs.length > 0) {
      settings.push('captions');
    }

    var plyrConfig = {
      controls: controls,
      settings: settings,
      speed: { selected: 1, options: [0.5, 0.75, 1, 1.25, 1.5, 2] },
      quality: {
        default: 720,
        options: [1080, 720, 480],
        forced: true,
        onChange: function(q) {}
      },
      keyboard: { focused: true, global: true },
      tooltips: { controls: true, seek: true },
      invertTime: true,
      toggleInvert: true,
      hideControls: true,
      clickToPlay: true,
      ratio: '16:9',
    };

    // Thumbnail preview — check if ready, retry if generating
    var thumbSrc = await getThumbnailSrc(filePath);
    if (thumbSrc) {
      plyrConfig.previewThumbnails = { enabled: true, src: thumbSrc };
    }

    // Captions config if subs available
    if (subs && subs.length > 0) {
      plyrConfig.captions = { active: false, language: 'auto', update: true };
    }

    plyrInstance = new Plyr('#player', plyrConfig);
    window._player = plyrInstance;

    plyrInstance.on('ready', function() {
      // Force invertTime — HLS attaches before Plyr, config may not apply
      setTimeout(function() {
        var timeEl = document.querySelector('.plyr__time--current');
        if (timeEl) timeEl.click();
      }, 100);

      initResumeProgress(currentPath);
      initTouchGestures();
    });

    initProgressSaving(currentPath);
  }

  function loadPlayerDirect(filePath, streamUrl, subs) {
    var videoEl = document.getElementById('player');
    if (!videoEl) return;

    // Dùng src trực tiếp, không qua HLS.js
    videoEl.src = streamUrl;

    // Build controls: YouTube/Netflix style layout
    var controls = [
      'play-large',
      'play',
      'progress',
      'current-time',
      'mute',
      'volume',
      'captions',
      'settings',
      'pip',
      'fullscreen'
    ];
    var settings = ['speed'];
    if (subs && subs.length > 0) {
      settings.push('captions');
    }

    var plyrConfig = {
      controls: controls,
      settings: settings,
      speed: { selected: 1, options: [0.5, 0.75, 1, 1.25, 1.5, 2] },
      keyboard: { focused: true, global: true },
      tooltips: { controls: true, seek: true },
      invertTime: true,
      toggleInvert: true,
      hideControls: true,
      clickToPlay: true,
      ratio: '16:9',
      // NO previewThumbnails — direct stream may be transcoding, avoid CPU contention
    };

    // Captions config if subs available
    if (subs && subs.length > 0) {
      plyrConfig.captions = { active: false, language: 'auto', update: true };
    }

    plyrInstance = new Plyr('#player', plyrConfig);
    window._player = plyrInstance;

    plyrInstance.on('ready', function() {
      // Force invertTime — config may not apply on direct stream
      setTimeout(function() {
        var timeEl = document.querySelector('.plyr__time--current');
        if (timeEl) timeEl.click();
      }, 100);

      initResumeProgress(currentPath);
      initTouchGestures();
    });

    initProgressSaving(currentPath);
  }

  function destroyPlayer() {
    if (hlsInstance) { hlsInstance.destroy(); hlsInstance = null; }
    if (plyrInstance) { plyrInstance.destroy(); plyrInstance = null; }
    var videoEl = document.getElementById('player');
    if (videoEl) {
      // Remove subtitle tracks from previous session
      Array.from(videoEl.querySelectorAll('track')).forEach(function(t) { t.remove(); });
      videoEl.removeAttribute('src');
      videoEl.load();
    }
    hideStatusOverlay();
    hideResumeToast();
  }

  // ─── RESUME / WATCH PROGRESS ─────────────────────────────

  function initProgressSaving(videoId) {
    if (!plyrInstance) return;
    var saveTimer = null;

    plyrInstance.on('timeupdate', function() {
      clearTimeout(saveTimer);
      saveTimer = setTimeout(function() {
        if (!plyrInstance || !plyrInstance.duration) return;

        var pct = (plyrInstance.currentTime / plyrInstance.duration) * 100;

        if (pct >= 95) {
          clearProgress(videoId);
          return;
        }

        localStorage.setItem('vp_progress_' + videoId, JSON.stringify({
          currentTime: plyrInstance.currentTime,
          duration:    plyrInstance.duration,
          percentage:  pct,
          updatedAt:   Date.now()
        }));

      }, 5000);
    });

    plyrInstance.on('ended', function() {
      clearProgress(videoId);
    });
  }

  function initResumeProgress(videoId) {
    var saved = getProgress(videoId);
    if (!saved || saved.percentage <= 5 || saved.percentage >= 95) return;

    var timeEl = document.getElementById('resume-time');
    var toast  = document.getElementById('resume-toast');
    if (!toast || !timeEl) return;

    timeEl.textContent = formatTime(saved.currentTime);
    toast.classList.remove('hidden');

    var autoHide = setTimeout(function() {
      hideResumeToast();
    }, 8000);

    document.getElementById('btn-resume').onclick = function() {
      plyrInstance.currentTime = saved.currentTime;
      plyrInstance.play();
      hideResumeToast();
      clearTimeout(autoHide);
    };

    document.getElementById('btn-restart').onclick = function() {
      clearProgress(videoId);
      hideResumeToast();
      clearTimeout(autoHide);
    };
  }

  function hideResumeToast() {
    var toast = document.getElementById('resume-toast');
    if (!toast) return;
    toast.classList.add('toast-hiding');
    setTimeout(function() {
      toast.classList.add('hidden');
      toast.classList.remove('toast-hiding');
    }, 250);
  }

  // ─── MOBILE TOUCH GESTURES ─────────────────────────────

  function initTouchGestures() {
    var plyrEl = document.querySelector('.plyr');
    if (!plyrEl) return;

    var tapCount = 0;
    var tapTimer = null;
    var startX   = 0;

    plyrEl.addEventListener('touchstart', function(e) {
      startX = e.changedTouches[0].clientX;
    }, { passive: true });

    plyrEl.addEventListener('touchend', function(e) {
      var endX = e.changedTouches[0].clientX;
      // Bỏ qua nếu là swipe (di chuyển > 10px)
      if (Math.abs(endX - startX) > 10) return;

      tapCount++;

      if (tapCount === 1) {
        tapTimer = setTimeout(function() {
          tapCount = 0;
          // Single tap: toggle controls
          var controls = document.querySelector('.plyr__controls');
          if (controls) {
            controls.style.opacity =
              controls.style.opacity === '0' ? '' : '0';
          }
        }, 250);

      } else if (tapCount === 2) {
        clearTimeout(tapTimer);
        tapCount = 0;

        var rect = plyrEl.getBoundingClientRect();
        var tapX = e.changedTouches[0].clientX - rect.left;

        if (tapX < rect.width / 2) {
          plyrInstance.rewind(10);
          showSeekFlash('\u23ea -10s', 'left');
        } else {
          plyrInstance.forward(10);
          showSeekFlash('\u23e9 +10s', 'right');
        }
      }
    });
  }

  function showSeekFlash(text, side) {
    // Xóa flash cũ nếu còn
    var old = document.querySelector('.seek-flash');
    if (old) old.remove();

    var flash = document.createElement('div');
    flash.className = 'seek-flash seek-flash--' + side;
    flash.textContent = text;

    var plyrEl = document.querySelector('.plyr');
    if (plyrEl) plyrEl.appendChild(flash);

    setTimeout(function() {
      if (flash.parentNode) flash.remove();
    }, 700);
  }

  // ─── HLS API CALLS ───────────────────────────────────────
  function getAuthHeaders() {
    var key = localStorage.getItem('dp_key') || '';
    return { 'Authorization': 'Bearer ' + key };
  }

  async function fetchHlsStatus(filePath) {
    try {
      var res = await fetch('/api/media/hls/' + encodeFilePath(filePath), {
        headers: getAuthHeaders()
      });
      if (!res.ok) return { status: 'error' };
      return await res.json();
    } catch(e) { return { status: 'error' }; }
  }

  async function triggerHlsConvert(filePath) {
    try {
      await fetch('/api/media/hls/' + encodeFilePath(filePath), {
        method: 'POST',
        headers: getAuthHeaders()
      });
    } catch(e) {
      console.warn('Trigger convert failed:', e);
    }
  }

  function waitUntilReady(filePath) {
    return new Promise(function(resolve) {
      var interval = setInterval(async function() {
        var data = await fetchHlsStatus(filePath);

        // Cập nhật progress % nếu đang transcode
        if (data.status === 'transcoding' && data.progress) {
          var pct = data.progress.percent || '';
          showStatusOverlay('\u23f3 Đang xử lý video... ' + pct + '%');
        }

        if (data.status === 'ready') {
          clearInterval(interval);
          resolve(data);
        }
      }, 5000); // poll mỗi 5 giây
    });
  }

  // ─── THUMBNAIL HELPERS ──────────────────────────────────

  async function getThumbnailSrc(filePath) {
    var url = '/api/media/thumbnails/' + encodeFilePath(filePath);
    try {
      var res = await fetch(url, { headers: getAuthHeaders() });
      if (res.status === 202) {
        // Đang generate, thử lại sau 15 giây
        setTimeout(function() {
          if (plyrInstance) {
            fetch(url, { headers: getAuthHeaders() }).then(function(r) {
              if (r.ok) {
                plyrInstance.previewThumbnails = { enabled: true, src: url };
              }
            }).catch(function() {});
          }
        }, 15000);
        return null;
      }
      if (res.ok) return url;
      return null;
    } catch(e) { return null; }
  }

  // ─── SUBTITLE HELPERS ────────────────────────────────────

  async function fetchSubtitles(filePath) {
    try {
      var res = await fetch('/api/media/subtitles/' + encodeFilePath(filePath), {
        headers: getAuthHeaders()
      });
      if (!res.ok) return [];
      var subs = await res.json();
      // Validate: only keep entries with a valid src
      if (!Array.isArray(subs)) return [];
      return subs.filter(function(s) {
        return s && s.src && s.label;
      });
    } catch(e) { return []; }
  }

  function injectSubtitleTracks(subs) {
    var videoEl = document.getElementById('player');
    if (!videoEl || !subs || subs.length === 0) return;

    subs.forEach(function(sub) {
      var track = document.createElement('track');
      track.kind    = 'subtitles';
      track.label   = sub.label;
      track.srclang = sub.language || 'und';
      track.src     = sub.src;
      videoEl.appendChild(track);
    });
  }

  // ─── HELPERS ─────────────────────────────────────────────

  // Encode path nhưng giữ nguyên dấu /
  function encodeFilePath(path) {
    return path.split('/').map(encodeURIComponent).join('/');
  }

  function showStatusOverlay(msg) {
    var overlay = document.getElementById('player-status-overlay');
    var text    = document.getElementById('player-status-text');
    if (overlay) {
      if (text) text.textContent = msg;
      overlay.classList.remove('hidden');
    }
  }

  function hideStatusOverlay() {
    var overlay = document.getElementById('player-status-overlay');
    if (overlay) overlay.classList.add('hidden');
  }

})();

