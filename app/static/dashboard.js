/* Radio Logger - 2000 dashboard */
(() => {
  const $ = (id) => document.getElementById(id);
  const audio = $('audio');
  const S = {
    me: null, tz: undefined, streams: [], stream: null, date: null, day: null,
    mode: 'idle',            // idle | replay | live
    chunkIdx: -1, peaks: null, pendingSeek: null,
    npTrackId: null, liveTrack: null,
    actx: null, analyser: null, levels: [],
  };

  // ---------- helpers ----------
  const store = {
    get(k) { try { return localStorage.getItem('rl2000.' + k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem('rl2000.' + k, v); } catch { /* storage unavailable */ } },
  };
  async function api(path) {
    const r = await fetch(path, { credentials: 'same-origin' });
    if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  }
  function toast(msg, err) {
    const t = $('toast'); t.textContent = msg; t.className = 'toast show' + (err ? ' err' : '');
    clearTimeout(toast.h); toast.h = setTimeout(() => (t.className = 'toast'), 3500);
  }
  let fmtTime, fmtHM, fmtDate;
  function setupFormatters(tz) {
    const mk = (opts) => { try { return new Intl.DateTimeFormat('en-AU', { ...opts, timeZone: tz }); } catch { return new Intl.DateTimeFormat('en-AU', opts); } };
    const t = mk({ hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    const hm = mk({ hour: '2-digit', minute: '2-digit', hour12: false });
    const d = (() => { try { return new Intl.DateTimeFormat('en-CA', { timeZone: tz }); } catch { return new Intl.DateTimeFormat('en-CA'); } })();
    fmtTime = (ts) => t.format(new Date(ts * 1000));
    fmtHM = (ts) => hm.format(new Date(ts * 1000));
    fmtDate = (ts) => d.format(new Date(ts * 1000)); // YYYY-MM-DD in station time
  }
  const today = () => fmtDate(Date.now() / 1000);
  const shiftDate = (s, n) => { const d = new Date(s + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const initials = (n) => n.split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toUpperCase();
  const logoHtml = (s, cls = '') => s.logo ? `<img src="${esc(s.logo)}" alt="" class="${cls}">` : `<div class="ph ${cls}">${esc(initials(s.name))}</div>`;
  const chunkLen = (c) => c.in_progress ? Math.max(c.duration, S.day.now - c.start) : c.duration;

  // ---------- boot ----------
  async function boot() {
    S.me = await api('/api/me');
    S.tz = S.me.tz_name;
    setupFormatters(S.tz);
    $('who').textContent = S.me.username;
    if (S.me.role === 'admin') { $('adminLink').hidden = false; $('noStreamsAdmin').hidden = false; }
    S.streams = await api('/api/streams');
    if (!S.streams.length) { $('noStreams').hidden = false; return; }
    $('main').hidden = false;
    S.date = today();
    renderStations();
    const saved = Number(store.get('stream'));
    await selectStream(S.streams.find((s) => s.id === saved) || S.streams[0]);
    setInterval(refreshTick, 20000);
    requestAnimationFrame(frame);
  }

  function renderStations() {
    $('stations').innerHTML = S.streams.map((s) =>
      `<div class="station-tab ${S.stream && S.stream.id === s.id ? 'active' : ''}" data-id="${s.id}">
         ${logoHtml(s)}<div><div>${esc(s.name)}</div></div>
         <span class="pill ${s.status}"><span class="dot"></span></span></div>`).join('');
    document.querySelectorAll('.station-tab').forEach((el) =>
      el.addEventListener('click', () => selectStream(S.streams.find((s) => s.id === Number(el.dataset.id)))));
  }

  async function selectStream(s) {
    stopPlayback();
    S.stream = s; store.set('stream', s.id);
    renderStations();
    $('stLogo').innerHTML = logoHtml(s);
    $('stName').textContent = s.name;
    setStatusPill(s.status);
    $('np').classList.toggle('show', s.is_music);
    $('tracksPanel').hidden = !s.is_music;
    await loadDay(S.date);
  }

  function setStatusPill(state) {
    const p = $('stStatus');
    p.className = 'pill ' + state;
    p.lastElementChild.textContent = { recording: 'Recording', error: 'Recorder error', disabled: 'Disabled', stopped: 'Stopped', starting: 'Starting' }[state] || state;
  }

  // ---------- day data ----------
  async function loadDay(date, keepPlayback) {
    S.date = date;
    $('date').value = date;
    $('nextDay').disabled = date >= today();
    S.day = await api(`/api/streams/${S.stream.id}/day?date=${date}`);
    if (!keepPlayback && S.mode === 'replay') stopPlayback();
    renderTimeline();
    renderTracks();
    const hrs = S.day.chunks.reduce((a, c) => a + chunkLen(c), 0) / 3600;
    $('dayInfo').textContent = S.day.chunks.length
      ? `${S.day.chunks.length} chunks · ${hrs.toFixed(1)} h recorded${S.day.tracks.length ? ` · ${S.day.tracks.length} tracks` : ''}`
      : 'No recordings for this day';
  }

  function renderTimeline() {
    const tl = $('timeline'), d = S.day, span = d.day_end - d.day_start;
    tl.querySelectorAll('.chunk,.tick,.trk').forEach((e) => e.remove());
    const pct = (ts) => ((ts - d.day_start) / span) * 100;
    const frag = document.createDocumentFragment();
    for (let h = 0; h < 24; h += 3) {
      const e = document.createElement('div'); e.className = 'tick';
      e.style.left = pct(d.day_start + h * 3600) + '%'; e.textContent = String(h).padStart(2, '0') + ':00';
      frag.appendChild(e);
    }
    d.chunks.forEach((c) => {
      const e = document.createElement('div'); e.className = 'chunk' + (c.in_progress ? ' inprog' : '');
      e.style.left = pct(c.start) + '%'; e.style.width = Math.max(0.05, (chunkLen(c) / span) * 100) + '%';
      e.title = `${fmtHM(c.start)} – ${fmtHM(c.start + chunkLen(c))}`;
      frag.appendChild(e);
    });
    d.tracks.forEach((t) => {
      const e = document.createElement('div'); e.className = 'trk'; e.style.left = pct(t.ts) + '%';
      frag.appendChild(e);
    });
    tl.appendChild(frag);
  }

  function renderTracks() {
    if (!S.stream.is_music) return;
    const list = $('tracklist');
    if (!S.day.tracks.length) { list.innerHTML = '<div class="empty">No tracks logged for this day.</div>'; return; }
    list.innerHTML = [...S.day.tracks].reverse().map((t) =>
      `<div class="trow" data-ts="${t.ts}" data-id="${t.id}">
         <div class="tm">${fmtHM(t.ts)}</div>
         <div class="th" style="${t.artwork_url ? `background-image:url('${esc(t.artwork_url)}')` : ''}"></div>
         <div><div class="tt">${esc(t.title)}</div><div class="ta">${esc(t.artist)}${t.album ? ' · ' + esc(t.album) : ''}</div></div>
       </div>`).join('');
    list.querySelectorAll('.trow').forEach((r) => r.addEventListener('click', () => playAt(Number(r.dataset.ts) - 2)));
  }

  async function refreshTick() {
    try {
      S.streams = await api('/api/streams');
      const s = S.streams.find((x) => x.id === S.stream.id);
      if (s) setStatusPill(s.status);
      renderStations();
      if (S.date === today()) await loadDay(S.date, true);
      if (S.mode === 'live' && S.stream.is_music) await refreshLiveTrack();
    } catch (e) { /* transient */ }
  }

  // ---------- replay ----------
  function findChunk(ts) {
    const cs = S.day.chunks;
    let i = cs.findIndex((c) => ts >= c.start && ts < c.start + chunkLen(c));
    if (i >= 0) return { i, offset: ts - cs[i].start };
    i = cs.findIndex((c) => c.start > ts);
    return i >= 0 ? { i, offset: 0 } : null;
  }

  function playAt(ts) {
    const hit = findChunk(ts);
    if (!hit) { toast('Nothing recorded at that time'); return; }
    if (S.mode === 'live') stopPlayback();
    loadChunk(hit.i, hit.offset, true);
  }

  async function loadChunk(i, offset, autoplay) {
    const c = S.day.chunks[i];
    S.mode = 'replay'; S.chunkIdx = i; S.pendingSeek = offset;
    $('stMode').textContent = 'Replay'; $('stMode').className = 'pill';
    setLiveBtn(false);
    const url = `/api/streams/${S.stream.id}/audio/${S.date}/${c.file}`;
    audio.src = url + (c.in_progress ? `?t=${Date.now()}` : '');
    $('dl').href = url + '?download=1'; $('dl').hidden = false;
    $('waveLabel').textContent = `${S.date}  ${fmtTime(c.start)} – ${fmtTime(c.start + chunkLen(c))}${c.in_progress ? '  (recording)' : ''}`;
    S.peaks = null; $('waveMsg').textContent = 'Drawing waveform…'; $('waveMsg').hidden = false;
    if (autoplay) ensureAudioGraph();
    audio.load();
    if (autoplay) audio.play().catch(() => {});
    try {
      const p = await api(`/api/streams/${S.stream.id}/peaks/${S.date}/${c.file}`);
      if (S.chunkIdx === i && S.mode === 'replay') { S.peaks = p; $('waveMsg').hidden = true; }
    } catch (e) { $('waveMsg').textContent = 'Waveform unavailable'; }
  }

  audio.addEventListener('loadedmetadata', () => {
    if (S.pendingSeek != null && S.mode === 'replay') {
      try { audio.currentTime = S.pendingSeek; } catch { /* not seekable yet */ }
      S.pendingSeek = null;
    }
  });

  audio.addEventListener('ended', async () => {
    if (S.mode !== 'replay') return;
    const cur = S.day.chunks[S.chunkIdx];
    const endTs = cur.start + audio.currentTime;
    if (cur.in_progress || S.date === today()) await loadDay(S.date, true);
    const next = S.day.chunks.findIndex((c) => c.start > cur.start);
    if (next >= 0) return loadChunk(next, 0, true);
    if (cur.in_progress) return loadChunk(S.chunkIdx, endTs - cur.start, true); // file grew, continue
    if (S.date < today()) {
      await loadDay(shiftDate(S.date, 1));
      if (S.day.chunks.length) return loadChunk(0, 0, true);
    }
    toast('Reached the end of the recordings');
  });

  function currentTs() {
    if (S.mode === 'live') return Date.now() / 1000;
    if (S.mode === 'replay' && S.day.chunks[S.chunkIdx]) return S.day.chunks[S.chunkIdx].start + (audio.currentTime || 0);
    return null;
  }

  // ---------- live ----------
  function setLiveBtn(on) {
    const b = $('liveBtn'); b.classList.toggle('on', on);
    b.lastElementChild.textContent = on ? 'Stop live' : 'Listen live';
  }

  async function startLive() {
    stopPlayback();
    S.mode = 'live'; S.levels = [];
    setLiveBtn(true);
    $('stMode').textContent = 'LIVE'; $('stMode').className = 'pill live';
    $('dl').hidden = true; $('waveLabel').textContent = 'Live input';
    $('waveMsg').textContent = 'Connecting…'; $('waveMsg').hidden = false;
    ensureAudioGraph();
    audio.src = `/api/streams/${S.stream.id}/live?t=${Date.now()}`;
    audio.play().then(() => ($('waveMsg').hidden = true)).catch((e) => { toast('Could not start the live stream', true); stopPlayback(); });
    if (S.stream.is_music) refreshLiveTrack();
  }

  async function refreshLiveTrack() {
    S.liveTrack = await api(`/api/streams/${S.stream.id}/nowplaying`).catch(() => null);
  }

  function stopPlayback() {
    audio.pause(); audio.removeAttribute('src'); audio.load();
    S.mode = 'idle'; S.chunkIdx = -1; S.peaks = null; S.npTrackId = undefined;
    setLiveBtn(false);
    $('stMode').textContent = 'Replay'; $('stMode').className = 'pill';
    $('dl').hidden = true; $('waveLabel').textContent = '';
    $('waveMsg').textContent = 'Pick a time on the timeline above, or press Listen live.'; $('waveMsg').hidden = false;
    $('clockSub').innerHTML = '&nbsp;';
  }

  function ensureAudioGraph() {
    if (S.actx) { if (S.actx.state === 'suspended') S.actx.resume(); return; }
    try {
      S.actx = new (window.AudioContext || window.webkitAudioContext)();
      const src = S.actx.createMediaElementSource(audio);
      S.analyser = S.actx.createAnalyser(); S.analyser.fftSize = 2048;
      src.connect(S.analyser); S.analyser.connect(S.actx.destination);
    } catch (e) { S.actx = null; }
  }

  // ---------- drawing ----------
  function frame() {
    drawWave();
    updateHeads();
    requestAnimationFrame(frame);
  }

  function sizeCanvas(cv) {
    const dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
    if (cv.width !== w * dpr || cv.height !== h * dpr) { cv.width = w * dpr; cv.height = h * dpr; }
    const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); return [g, w, h];
  }

  function drawWave() {
    const cv = $('wave'); const [g, w, h] = sizeCanvas(cv);
    g.clearRect(0, 0, w, h);
    const mid = h / 2;
    g.fillStyle = '#27335e'; g.fillRect(0, mid, w, 1);
    const grad = g.createLinearGradient(0, 0, w, 0);
    grad.addColorStop(0, '#5b6cff'); grad.addColorStop(1, '#2ec4b6');

    if (S.mode === 'live' && S.analyser) {
      const buf = new Float32Array(S.analyser.fftSize); S.analyser.getFloatTimeDomainData(buf);
      let peak = 0; for (const v of buf) peak = Math.max(peak, Math.abs(v));
      S.levels.push(peak); const bars = Math.floor(w / 3); if (S.levels.length > bars) S.levels.splice(0, S.levels.length - bars);
      g.fillStyle = grad;
      S.levels.forEach((v, i) => { const bh = Math.max(1, v * (h - 20)); g.fillRect(w - (S.levels.length - i) * 3, mid - bh / 2, 2, bh); });
      return;
    }
    if (S.mode !== 'replay' || !S.peaks) return;
    const pk = S.peaks.peaks, dur = pk.length / S.peaks.per_second || 1;
    const bw = 2, gap = 1, n = Math.floor(w / (bw + gap));
    const played = (audio.currentTime || 0) / dur;
    for (let i = 0; i < n; i++) {
      const a = Math.floor((i / n) * pk.length), b = Math.max(a + 1, Math.floor(((i + 1) / n) * pk.length));
      let v = 0; for (let j = a; j < b && j < pk.length; j++) v = Math.max(v, pk[j]);
      const bh = Math.max(1, Math.sqrt(v) * (h - 24));
      g.fillStyle = i / n <= played ? grad : '#3a4a80';
      g.fillRect(i * (bw + gap), mid - bh / 2, bw, bh);
    }
    // track change markers within this chunk
    const c = S.day.chunks[S.chunkIdx];
    if (c && S.stream.is_music) {
      g.fillStyle = '#ffb547'; g.font = '11px ui-monospace, monospace';
      S.day.tracks.forEach((t) => {
        if (t.ts >= c.start && t.ts < c.start + dur) {
          const x = ((t.ts - c.start) / dur) * w; g.fillRect(x, 22, 1, h - 30);
          g.fillText((t.title || '').slice(0, 24), x + 4, h - 8);
        }
      });
    }
    const x = played * w; g.fillStyle = '#fff'; g.fillRect(x, 0, 2, h);
  }

  function updateHeads() {
    const ts = currentTs();
    const head = $('tlHead');
    if (ts && S.day && ts >= S.day.day_start && ts < S.day.day_end) {
      head.style.display = 'block'; head.style.left = ((ts - S.day.day_start) / (S.day.day_end - S.day.day_start)) * 100 + '%';
    } else head.style.display = 'none';
    $('clock').textContent = ts ? fmtTime(ts) : '--:--:--';
    if (S.mode === 'replay') $('clockSub').textContent = `${S.date} · ${audio.paused ? 'paused' : 'playing'}`;
    else if (S.mode === 'live') $('clockSub').textContent = 'live · ' + (audio.paused ? 'buffering' : 'on air');
    $('playIcon').innerHTML = audio.paused ? '<path d="M8 5v14l11-7z"/>' : '<path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/>';
    if (S.stream && S.stream.is_music) updateNowPlaying(ts);
  }

  function updateNowPlaying(ts) {
    let t = null;
    if (S.mode === 'live') t = S.liveTrack && S.liveTrack.id ? S.liveTrack : null;
    else if (ts && S.day) { for (const x of S.day.tracks) { if (x.ts <= ts) t = x; else break; } }
    const id = t ? t.id : null;
    if (id === S.npTrackId) return;
    S.npTrackId = id;
    $('npHead').textContent = S.mode === 'live' ? 'Now playing' : (t ? `Playing at ${fmtHM(t.ts)}` : 'Now playing');
    $('npTitle').textContent = t ? t.title : '—';
    $('npArtist').textContent = t ? t.artist : '';
    $('npAlbum').textContent = t && t.album ? t.album : '';
    const art = $('npArt');
    art.style.backgroundImage = t && t.artwork_url ? `url('${t.artwork_url.replace(/'/g, '%27')}')` : '';
    art.firstElementChild.style.display = t && t.artwork_url ? 'none' : '';
    document.querySelectorAll('.trow').forEach((r) => r.classList.toggle('current', Number(r.dataset.id) === id));
  }

  // ---------- inputs ----------
  const tl = $('timeline');
  const tsFromEvent = (e) => { const r = tl.getBoundingClientRect(); return S.day.day_start + ((e.clientX - r.left) / r.width) * (S.day.day_end - S.day.day_start); };
  tl.addEventListener('click', (e) => playAt(tsFromEvent(e)));
  tl.addEventListener('mousemove', (e) => {
    const hv = $('tlHover'), r = tl.getBoundingClientRect();
    hv.style.display = 'block'; hv.style.left = e.clientX - r.left + 'px';
    hv.firstElementChild.textContent = fmtHM(tsFromEvent(e));
  });
  tl.addEventListener('mouseleave', () => ($('tlHover').style.display = 'none'));

  $('waveWrap').addEventListener('click', (e) => {
    if (S.mode !== 'replay' || !S.peaks) return;
    const r = e.currentTarget.getBoundingClientRect(), dur = S.peaks.peaks.length / S.peaks.per_second;
    audio.currentTime = ((e.clientX - r.left) / r.width) * dur;
  });

  const seekBy = (s) => { if (S.mode === 'replay') audio.currentTime = Math.max(0, audio.currentTime + s); };
  $('back').onclick = () => seekBy(-10);
  $('fwd').onclick = () => seekBy(10);
  $('prevChunk').onclick = () => { if (S.mode === 'replay' && S.chunkIdx > 0) loadChunk(S.chunkIdx - 1, 0, true); };
  $('nextChunk').onclick = () => { if (S.mode === 'replay' && S.chunkIdx < S.day.chunks.length - 1) loadChunk(S.chunkIdx + 1, 0, true); };
  $('play').onclick = () => {
    if (S.mode === 'idle') {
      if (S.day.chunks.length) return loadChunk(0, 0, true);
      return toast('Nothing recorded on this day — try Listen live');
    }
    ensureAudioGraph();
    audio.paused ? audio.play().catch(() => {}) : audio.pause();
  };
  $('liveBtn').onclick = () => (S.mode === 'live' ? stopPlayback() : startLive());
  $('vol').oninput = (e) => { audio.volume = Number(e.target.value); store.set('vol', e.target.value); };
  const v = store.get('vol'); if (v) { audio.volume = Number(v); $('vol').value = v; }

  $('date').addEventListener('change', (e) => e.target.value && loadDay(e.target.value));
  $('prevDay').onclick = () => loadDay(shiftDate(S.date, -1));
  $('nextDay').onclick = () => S.date < today() && loadDay(shiftDate(S.date, 1));
  $('todayBtn').onclick = () => loadDay(today());

  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT') return;
    if (e.code === 'Space') { e.preventDefault(); $('play').click(); }
    if (e.code === 'ArrowLeft') seekBy(-10);
    if (e.code === 'ArrowRight') seekBy(10);
  });

  boot().catch((e) => toast(e.message, true));
})();
