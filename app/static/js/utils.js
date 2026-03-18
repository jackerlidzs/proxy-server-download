// Format seconds → "1:23:45" hoặc "12:34"
function formatTime(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) {
    return `${h}:${m.toString().padStart(2,'0')}:${s.toString().padStart(2,'0')}`;
  }
  return `${m}:${s.toString().padStart(2,'0')}`;
}

function getProgress(videoId) {
  try {
    return JSON.parse(localStorage.getItem(`vp_progress_${videoId}`));
  } catch { return null; }
}

function clearProgress(videoId) {
  localStorage.removeItem(`vp_progress_${videoId}`);
}
