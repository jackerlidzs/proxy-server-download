(function() {
  'use strict';

  // Xóa Plyr localStorage cũ (nếu có) — không cho Plyr lưu settings
  localStorage.removeItem('plyr');

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

      // Detect Vietnamese subtitle for default language
      var viTrack = subs.find(function(s) {
        return s.language === 'vi' || s.language === 'vie';
      });
      window._defaultSubLang = viTrack ? 'vi' : null;

      if (statusData.status === 'ready') {
        // HLS đã sẵn sàng → dùng luôn (seek instant, nhẹ server)
        loadPlayer(filePath, statusData.master_url, subs);

      } else if (statusData.status === 'transcoding') {
        // Đang convert → chờ với timeout, fallback nếu quá lâu
        var directUrl = fallbackStreamUrl || ('/stream-transcode/' + encodeFilePath(filePath));
        showStatusOverlay('\u23f3 Đang xử lý video...');
        try {
          var readyData = await waitUntilReady(filePath);
          hideStatusOverlay();
          if (readyData && readyData.fallback) {
            await loadPlayerDirect(filePath, directUrl, subs);
          } else {
            loadPlayer(filePath, readyData.master_url, subs);
          }
        } catch(err) {
          hideStatusOverlay();
          await loadPlayerDirect(filePath, directUrl, subs);
        }

      } else {
        // not_started / error / unknown → play NGAY qua direct stream
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
      storage: { enabled: false, key: 'plyr' },
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

      // Restore saved volume
      restoreVolume();

      // Set default subtitle language to 'vi' if available
      if (window._defaultSubLang && plyrInstance.captions) {
        plyrInstance.language = window._defaultSubLang;
      }
      window._defaultSubLang = null;

      initResumeProgress(currentPath);
      initTouchGestures();
    });

    // Save volume on change
    plyrInstance.on('volumechange', function() {
      saveVolume();
    });

    initProgressSaving(currentPath);
  }

  async function loadPlayerDirect(filePath, streamUrl, subs) {
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
      storage: { enabled: false, key: 'plyr' },
      speed: { selected: 1, options: [0.5, 0.75, 1, 1.25, 1.5, 2] },
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
      // Force invertTime — config may not apply on direct stream
      setTimeout(function() {
        var timeEl = document.querySelector('.plyr__time--current');
        if (timeEl) timeEl.click();
      }, 100);

      // Restore saved volume
      restoreVolume();

      // Set default subtitle language to 'vi' if available
      if (window._defaultSubLang && plyrInstance.captions) {
        plyrInstance.language = window._defaultSubLang;
      }
      window._defaultSubLang = null;

      initResumeProgress(currentPath);
      initTouchGestures();
    });

    // Save volume on change
    plyrInstance.on('volumechange', function() {
      saveVolume();
    });

    initProgressSaving(currentPath);
  }

  function destroyPlayer() {
    // Clear HLS polling nếu đang chạy
    if (window._hlsPollingInterval) {
      clearInterval(window._hlsPollingInterval);
      window._hlsPollingInterval = null;
    }
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
    var strategyLabels = {
      'copy':       'Copy stream',
      'audio_only': 'Convert audio',
      're-encode':  'Re-encode video',
    };
    var MAX_WAIT = 30 * 60 * 1000; // 30 phút tối đa
    var startTime = Date.now();

    return new Promise(function(resolve, reject) {
      var interval = setInterval(async function() {
        // Timeout check
        if (Date.now() - startTime > MAX_WAIT) {
          clearInterval(interval);
          window._hlsPollingInterval = null;
          reject(new Error('HLS convert timeout'));
          return;
        }

        var data = await fetchHlsStatus(filePath);

        if (data.status === 'transcoding' && data.progress) {
          var pct = data.progress.percent || 0;
          var strategy = data.strategy || '';
          var label = strategyLabels[strategy] || 'Converting';
          var eta = data.eta_minutes || 0;
          var elapsed = data.elapsed_minutes || 0;
          var remaining = Math.max(0, Math.round(eta - elapsed));

          var msg = '\u23f3 ' + label + '... ' + pct + '%';
          if (remaining > 0) {
            msg += '\nCòn khoảng ' + remaining + ' phút';
          }
          showStatusOverlay(msg);
        }

        if (data.status === 'queued') {
          showStatusOverlay('\u23f3 Đang chờ server... (có video khác đang convert)');
        }

        if (data.status === 'error') {
          clearInterval(interval);
          window._hlsPollingInterval = null;
          showStatusOverlay('\u274c Convert thất bại. Thử stream trực tiếp...');
          setTimeout(function() { resolve({ fallback: true }); }, 2000);
        }

        if (data.status === 'ready') {
          clearInterval(interval);
          window._hlsPollingInterval = null;
          resolve(data);
        }
      }, 5000);

      // Lưu interval ID để cancel từ destroyPlayer()
      window._hlsPollingInterval = interval;
    });
  }

  // ─── THUMBNAIL HELPERS ──────────────────────────────────

  async function getThumbnailSrc(filePath) {
    var url = '/api/media/thumbnails/' + encodeFilePath(filePath);
    try {
      // Fetch với redirect follow để lấy final URL
      var res = await fetch(url, { redirect: 'follow' });

      if (res.status === 202) {
        // Đang generate, retry sau 15 giây
        setTimeout(async function() {
          try {
            var res2 = await fetch(url, { redirect: 'follow' });
            if (res2.ok && plyrInstance) {
              // res2.url = final URL sau redirect (e.g. /thumbnails/hash/index.vtt)
              plyrInstance.previewThumbnails.src     = res2.url;
              plyrInstance.previewThumbnails.enabled = true;
            }
          } catch(e) {
            console.warn('Thumbnail retry failed:', e);
          }
        }, 15000);
        return null;
      }

      // res.url = final URL sau redirect
      // Ví dụ: /thumbnails/931dd982a2d0/index.vtt
      if (res.ok) return res.url;
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

  // ─── VOLUME HELPERS ─────────────────────────────────────

  function saveVolume() {
    if (!plyrInstance) return;
    localStorage.setItem('vp_vol', JSON.stringify({
      volume: plyrInstance.volume,
      muted: plyrInstance.muted
    }));
  }

  function restoreVolume() {
    if (!plyrInstance) return;
    try {
      var saved = JSON.parse(localStorage.getItem('vp_vol'));
      if (saved) {
        plyrInstance.volume = saved.volume;
        plyrInstance.muted = saved.muted;
      }
    } catch(e) {}
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

