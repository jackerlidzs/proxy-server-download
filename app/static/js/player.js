(function() {
  'use strict';

  let plyrInstance  = null;
  let hlsInstance   = null;
  let currentPath   = null;

  // ─── PUBLIC API ──────────────────────────────────────────
  // app.js gọi hàm này thay cho VP.play() cũ
  // Ví dụ: window.PlayerModule.open('movies/BigBuck.mkv', 'Big Buck Bunny')

  window.PlayerModule = {

    open: async function(filePath, fileName) {
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

      // Check HLS status
      var statusData = await fetchHlsStatus(filePath);

      if (statusData.status === 'ready') {
        // HLS đã sẵn sàng → dùng luôn (seek instant, nhẹ server)
        loadPlayer(filePath, statusData.master_url);

      } else {
        // HLS chưa có → play NGAY qua stream-transcode
        // Không block, không chờ
        var streamUrl = '/stream-transcode/' + encodeFilePath(filePath);
        loadPlayerDirect(filePath, streamUrl);

        // Trigger HLS convert ngầm (fire and forget)
        if (statusData.status === 'not_started') {
          triggerHlsConvert(filePath);
        }
        // Không cần waitUntilReady — lần sau mở lại sẽ dùng HLS
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

  function loadPlayer(filePath, masterUrl) {
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

    plyrInstance = new Plyr('#player', {
      controls: [
        'play-large', 'play', 'rewind', 'fast-forward',
        'progress', 'current-time', 'duration',
        'mute', 'volume', 'settings', 'fullscreen'
      ],
      settings: ['speed', 'quality'],
      speed: { selected: 1, options: [0.5, 0.75, 1, 1.25, 1.5, 2] },
      quality: {
        default: 720,
        options: [1080, 720, 480],
        forced: true,
        onChange: function(q) {
          // hls.js handle quality switching tự động
        }
      },
      keyboard: { focused: true, global: true },
      tooltips: { controls: true, seek: true },
      hideControls: true,
      clickToPlay: true,
      rewind: 10,
      fastForward: 10,
    });

    window._player = plyrInstance;

    plyrInstance.on('ready', function() {
      initResumeProgress(currentPath);
      initPiP();
      initTouchGestures();
    });

    initProgressSaving(currentPath);
  }

  function loadPlayerDirect(filePath, streamUrl) {
    var videoEl = document.getElementById('player');
    if (!videoEl) return;

    // Dùng src trực tiếp, không qua HLS.js
    videoEl.src = streamUrl;

    plyrInstance = new Plyr('#player', {
      controls: [
        'play-large', 'play', 'rewind', 'fast-forward',
        'progress', 'current-time', 'duration',
        'mute', 'volume', 'settings', 'fullscreen'
      ],
      settings: ['speed'],
      speed: { selected: 1, options: [0.5, 0.75, 1, 1.25, 1.5, 2] },
      keyboard: { focused: true, global: true },
      tooltips: { controls: true, seek: true },
      hideControls: true,
      clickToPlay: true,
      rewind: 10,
      fastForward: 10,
    });

    window._player = plyrInstance;

    plyrInstance.on('ready', function() {
      initResumeProgress(currentPath);
      initPiP();
      initTouchGestures();
    });

    initProgressSaving(currentPath);
  }

  function destroyPlayer() {
    // Thoát PiP nếu đang active
    if (document.pictureInPictureElement) {
      document.exitPictureInPicture().catch(function(){});
    }
    var pipOverlay = document.getElementById('pip-overlay');
    if (pipOverlay) pipOverlay.classList.add('hidden');
    var pipBtn = document.getElementById('pip-btn');
    if (pipBtn) pipBtn.remove();

    if (hlsInstance) { hlsInstance.destroy(); hlsInstance = null; }
    if (plyrInstance) { plyrInstance.destroy(); plyrInstance = null; }
    var videoEl = document.getElementById('player');
    if (videoEl) {
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

  // ─── PICTURE IN PICTURE ───────────────────────────────

  function initPiP() {
    if (!document.pictureInPictureEnabled) return;
    if (!plyrInstance) return;

    var video = plyrInstance.media;

    // Tạo nút PiP
    var pipBtn = document.createElement('button');
    pipBtn.type = 'button';
    pipBtn.id   = 'pip-btn';
    pipBtn.className = 'plyr__controls__item plyr__control pip-control';
    pipBtn.setAttribute('aria-label', 'Picture in Picture');
    pipBtn.setAttribute('title', 'Picture in Picture');
    pipBtn.innerHTML =
      '<svg xmlns="http://www.w3.org/2000/svg"' +
      ' viewBox="0 0 24 24" width="18" height="18" fill="currentColor">' +
      '<path d="M21 3H3c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h18' +
      'c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 16H3V5h18v14z' +
      'm-10-7h9v6h-9z"/>' +
      '</svg>';

    // Inject trước nút fullscreen
    var fullscreenBtn = document.querySelector(
      '.plyr__controls [data-plyr="fullscreen"]'
    );
    if (fullscreenBtn) {
      fullscreenBtn.parentNode.insertBefore(pipBtn, fullscreenBtn);
    } else {
      var controls = document.querySelector('.plyr__controls');
      if (controls) controls.appendChild(pipBtn);
    }

    // Toggle PiP khi click
    pipBtn.addEventListener('click', async function() {
      try {
        if (document.pictureInPictureElement) {
          await document.exitPictureInPicture();
        } else {
          await video.requestPictureInPicture();
        }
      } catch(err) {
        console.warn('PiP error:', err);
      }
    });

    // Enter PiP
    video.addEventListener('enterpictureinpicture', function() {
      pipBtn.classList.add('plyr__control--pressed');
      var overlay = document.getElementById('pip-overlay');
      if (overlay) overlay.classList.remove('hidden');
    });

    // Leave PiP
    video.addEventListener('leavepictureinpicture', function() {
      pipBtn.classList.remove('plyr__control--pressed');
      var overlay = document.getElementById('pip-overlay');
      if (overlay) overlay.classList.add('hidden');
    });

    // Nút "Quay lại" trong overlay
    var pipReturn = document.getElementById('pip-return');
    if (pipReturn) {
      pipReturn.addEventListener('click', function() {
        document.exitPictureInPicture();
      });
    }
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

