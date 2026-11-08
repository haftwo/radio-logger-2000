/* Radio Logger - 2000 dashboard */
(() => {
  const $ = (id) => document.getElementById(id);
  const audio = $('audio');
  const S = {
    me: null, tz: undefined, streams: [], stream: null, date: null, day: null,
    mode: 'idle',            // idle | replay | live
    chunkIdx: -1, peaks: null, pendingSeek: null, scrubTs: null,
    markIn: null, markOut: null, clipStopAt: null,   // clip points (epoch seconds)
    npTrackId: null, liveTrack: null,
    actx: null, analyser: null, levels: [],
    zoom: 1, viewStart: 0, follow: true, lastPan: 0,
    tlStart: null, tlSpan: null, tlFollow: true, tlLastPan: 0,   // timeline view window (seconds)
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
  const hueOf = (str) => { let h = 0; for (const ch of str || '') h = (h * 31 + ch.charCodeAt(0)) >>> 0; return h % 360; };
  const songColor = (t, a = 1) => `hsla(${hueOf((t.artist || '') + (t.title || ''))}, 65%, 58%, ${a})`;
  // a song lasts until the next one starts, or for its known length, whichever is sooner
  function songEnd(i) {
    const tr = S.day.tracks, t = tr[i], next = tr[i + 1] ? tr[i + 1].ts : Infinity;
    const byLen = t.duration ? t.ts + t.duration + 5 : t.ts + 600;
    return Math.min(next, byLen, Math.max(S.day.now, t.ts + 30));
  }
  const chipsHtml = (t, withSrc) => (t.tags || []).map((g) => `<span class="chip ${/^\d{4}$/.test(g) ? 'year' : ''}">${esc(g)}</span>`).join('')
    + (withSrc && t.source && t.source !== 'icy' && t.source !== 'shazam' ? `<span class="chip src">${esc(t.source.split(':')[0])}</span>` : '');
  // chunk "start" is where its audio really sits; "named" is the file time ffmpeg opened it at
  const chunkLen = (c) => c.in_progress ? Math.max(c.duration, S.day.now - (c.named ?? c.start)) : c.duration;

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
    const qp = new URLSearchParams(location.search);
    const saved = Number(qp.get('stream') || store.get('stream'));
    if (qp.get('date') && /^\d{4}-\d{2}-\d{2}$/.test(qp.get('date'))) S.date = qp.get('date');
    await selectStream(S.streams.find((s) => s.id === saved) || S.streams[0]);
    if (qp.get('time')) { $('gotoTime').value = qp.get('time'); gotoDateTime(S.date, qp.get('time')); }
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
    S.markIn = S.markOut = null; updateClipUI();
    S.stream = s; store.set('stream', s.id);
    renderStations();
    $('stLogo').innerHTML = logoHtml(s);
    $('stName').textContent = s.name;
    renderStationInfo(s);
    setStatusPill(s.status);
    $('np').classList.toggle('show', s.is_music);
    $('tracksPanel').hidden = !s.is_music;
    $('transcriptPanel').hidden = s.is_music;
    S.tr = null; S.hits = []; $('trQuery').value = '';
    await loadDay(S.date);
  }

  function renderStationInfo(s) {
    const st = s.station || {}, bits = [];
    if (st.name && st.name.toLowerCase() !== s.name.toLowerCase()) bits.push(`On air as <b>${esc(st.name)}</b>`);
    if (st.genre) bits.push(esc(st.genre));
    if (st.description && st.description !== st.name) bits.push(esc(st.description));
    $('stInfo').innerHTML = bits.join(' · ');
    $('stInfo').hidden = !bits.length;
  }

  function setStatusPill(state) {
    const p = $('stStatus');
    p.className = 'pill ' + state;
    p.lastElementChild.textContent = { recording: 'Recording', error: 'Recorder error', disabled: 'Disabled', stopped: 'Stopped', starting: 'Starting' }[state] || state;
  }

  // ---------- day data ----------
  async function loadDay(date, keepPlayback) {
    if (date !== S.date) S.tlSpan = null;
    S.date = date;
    $('date').value = date;
    $('nextDay').disabled = date >= today();
    S.day = await api(`/api/streams/${S.stream.id}/day?date=${date}`);
    if (!keepPlayback && S.mode === 'replay') stopPlayback();
    renderTimeline();
    renderTracks();
    updateExportLinks();
    if (S.stream.is_music) loadCharts();
    else loadTranscript(keepPlayback);
    const hrs = S.day.chunks.reduce((a, c) => a + chunkLen(c), 0) / 3600;
    $('dayInfo').textContent = S.day.chunks.length
      ? `${S.day.chunks.length} chunks · ${hrs.toFixed(1)} h recorded${S.day.tracks.length ? ` · ${S.day.tracks.length} tracks` : ''}`
      : 'No recordings for this day';
  }

  // ---- timeline view (zoomable from the whole day down to 5 minutes) ----
  const TL_MIN = 300;
  function tlView() {
    const d = S.day, full = d.day_end - d.day_start;
    if (S.tlSpan == null || S.tlSpan > full) { S.tlSpan = full; S.tlStart = d.day_start; }
    S.tlSpan = Math.max(TL_MIN, S.tlSpan);
    S.tlStart = Math.min(Math.max(d.day_start, S.tlStart), d.day_end - S.tlSpan);
    return { start: S.tlStart, span: S.tlSpan, full };
  }
  function tlZoom(factor, anchorFrac = 0.5) {
    if (!S.day) return;
    const v = tlView(), anchor = v.start + anchorFrac * v.span;
    S.tlSpan = Math.min(v.full, Math.max(TL_MIN, v.span * factor));
    S.tlStart = anchor - anchorFrac * S.tlSpan;
    renderTimeline();
  }
  function tlFit() { S.tlSpan = null; renderTimeline(); }
  function fmtSpan(sec) { return sec >= 3600 ? +(sec / 3600).toFixed(sec % 3600 ? 1 : 0) + ' h' : Math.round(sec / 60) + ' min'; }

  let tlPending = false;
  function renderTimelineSoon() { if (!tlPending) { tlPending = true; requestAnimationFrame(() => { tlPending = false; renderTimeline(); }); } }

  function renderTimeline() {
    const tl = $('timeline'), d = S.day;
    tl.querySelectorAll('.chunk,.tick,.trk,.song,.hit').forEach((e) => e.remove());
    const v = tlView(), vEnd = v.start + v.span;
    const pct = (ts) => ((ts - v.start) / v.span) * 100;
    const frag = document.createDocumentFragment();
    // ticks every 1 min … 3 h, at least ~70 px apart
    const steps = [60, 300, 600, 900, 1800, 3600, 7200, 10800];
    const step = steps.find((st) => (st / v.span) * tl.clientWidth >= 70) || 10800;
    for (let t = Math.ceil((v.start - d.day_start) / step) * step + d.day_start; t < vEnd; t += step) {
      const e = document.createElement('div'); e.className = 'tick';
      e.style.left = pct(t) + '%'; e.textContent = fmtHM(t);
      frag.appendChild(e);
    }
    d.chunks.forEach((c) => {
      const end = c.start + chunkLen(c);
      if (end < v.start || c.start > vEnd) return;
      const e = document.createElement('div'); e.className = 'chunk' + (c.in_progress ? ' inprog' : '');
      e.style.left = pct(c.start) + '%'; e.style.width = Math.max(0.05, ((end - c.start) / v.span) * 100) + '%';
      e.title = `${fmtHM(c.start)} – ${fmtHM(end)}`;
      frag.appendChild(e);
    });
    d.tracks.forEach((t, i) => {
      const end = songEnd(i);
      if (end < v.start || t.ts > vEnd) return;
      const e = document.createElement('div'); e.className = 'song';
      e.dataset.ts = t.ts;
      e.style.left = pct(t.ts) + '%';
      e.style.width = Math.max(0.12, ((end - t.ts) / v.span) * 100) + '%';
      e.style.background = songColor(t);
      e.title = `${fmtHM(t.ts)}  ${t.artist ? t.artist + ' – ' : ''}${t.title}${t.tags && t.tags.length ? '  [' + t.tags.join(', ') + ']' : ''}`;
      // when zoomed in far enough, label the song right on the timeline
      if ((end - t.ts) / v.span * tl.clientWidth > 90) {
        e.classList.add('wide'); e.textContent = `${t.artist ? t.artist + ' – ' : ''}${t.title}`;
      }
      frag.appendChild(e);
    });
    tl.appendChild(frag);
    (S.hits || []).forEach((h) => {
      if (h.ts < v.start || h.ts > vEnd) return;
      const e = document.createElement('div'); e.className = 'hit'; e.style.left = pct(h.ts) + '%';
      tl.appendChild(e);
    });
    tl.classList.toggle('zoomed', v.span < v.full);
    $('tlZoomLvl').textContent = v.span >= v.full ? '24 h' : fmtSpan(v.span);
  }

  function renderTracks() {
    if (!S.stream.is_music) return;
    const list = $('tracklist');
    if (!S.day.tracks.length) { list.innerHTML = '<div class="empty">No tracks logged for this day.</div>'; return; }
    list.innerHTML = [...S.day.tracks].reverse().map((t) =>
      `<div class="trow" data-ts="${t.ts}" data-id="${t.id}">
         <div class="tm">${fmtHM(t.ts)}</div>
         <div class="th" style="${t.artwork_url ? `background-image:url('${esc(t.artwork_url)}')` : ''}"></div>
         <div><div class="tt">${esc(t.title)}</div><div class="ta">${esc(t.artist)}${t.album ? ' · ' + esc(t.album) : ''}</div>
           <div class="chips">${chipsHtml(t, true)}</div></div>
         <button class="btn sm edit" data-edit="${t.id}" title="Edit tags">Edit</button>
       </div>`).join('');
    list.querySelectorAll('.trow').forEach((r) => r.addEventListener('click', (e) => {
      if (e.target.closest('[data-edit]')) return openTrack(S.day.tracks.find((t) => t.id === Number(r.dataset.id)));
      playAt(Number(r.dataset.ts) - 2);
    }));
  }

  // ---------- track panel tabs: played list | top played ----------
  S.trTab = store.get('trTab') === 'charts' ? 'charts' : 'played';
  function setTrackTab(name) {
    S.trTab = name; store.set('trTab', name);
    document.querySelectorAll('#tracksPanel .ptab').forEach((b) => {
      const on = b.dataset.tab === name;
      b.classList.toggle('active', on); b.setAttribute('aria-selected', on);
    });
    $('tracklist').hidden = name !== 'played';
    $('charts').hidden = name !== 'charts';
    $('tagHere').hidden = name !== 'played';
    if (name === 'charts') loadCharts();
  }
  document.querySelectorAll('#tracksPanel .ptab').forEach((b) => (b.onclick = () => setTrackTab(b.dataset.tab)));
  setTrackTab(S.trTab);

  function updateExportLinks() {
    if (!S.stream || !S.date) return;
    const base = `/api/streams/${S.stream.id}/tracks.csv`;
    $('expDay').href = `${base}?range=day&date=${S.date}`;
    $('expWeek').href = `${base}?range=week&date=${S.date}`;
    $('expAll').href = `${base}?range=all`;
  }
  $('exportMenu').addEventListener('click', (e) => { if (e.target.closest('a')) $('exportMenu').open = false; });
  document.addEventListener('click', (e) => { if (!e.target.closest('#exportMenu')) $('exportMenu').open = false; });

  async function loadCharts() {
    if (!S.stream || !S.stream.is_music || S.trTab !== 'charts') return;
    const want = `${S.stream.id}|${S.date}`;
    let c;
    try { c = await api(`/api/streams/${S.stream.id}/charts?date=${S.date}`); } catch (e) { return toast(e.message, true); }
    if (want !== `${S.stream.id}|${S.date}`) return; // user moved on while this was loading
    renderChart($('chDay'), $('chDayMeta'), c.day, 'this day');
    const wk = `${fmtShort(c.week.start)} – ${fmtShort(c.week.end - 1)}`;
    renderChart($('chWeek'), $('chWeekMeta'), c.week, 'these 7 days', wk);
  }
  const fmtShort = (ts) => { const [, m, d] = fmtDate(ts).split('-'); return `${Number(d)}/${Number(m)}`; };

  function renderChart(list, meta, data, label, range) {
    meta.textContent = (range ? range + ' · ' : '') + (data.plays ? `${data.plays} plays · ${data.songs} songs` : '');
    if (!data.top.length) { list.innerHTML = `<li class="empty">No songs logged ${label}.</li>`; return; }
    const max = data.top[0].plays;
    list.innerHTML = data.top.map((t, i) =>
      `<li class="crow" data-ts="${t.last_ts}" title="Play the most recent time this was played (${fmtDate(t.last_ts)} ${fmtHM(t.last_ts)})">
         <div class="rk">${i + 1}</div>
         <div class="th" style="${t.artwork_url ? `background-image:url('${esc(t.artwork_url)}')` : ''}"></div>
         <div style="min-width:0"><div class="tt">${esc(t.title)}</div><div class="ta">${esc(t.artist)}</div>
           <div class="bar" style="width:${Math.max(6, (t.plays / max) * 100)}%"></div></div>
         <div class="pc"><b>${t.plays}</b> ${t.plays === 1 ? 'play' : 'plays'}</div>
       </li>`).join('');
    list.querySelectorAll('.crow').forEach((r) => (r.onclick = () => jumpTo(Number(r.dataset.ts))));
  }

  // play from any moment, switching day if needed
  async function jumpTo(ts) {
    const d = fmtDate(ts);
    if (d !== S.date) await loadDay(d);
    playAt(ts - 2);
  }

  async function refreshTick() {
    try {
      S.streams = await api('/api/streams');
      const s = S.streams.find((x) => x.id === S.stream.id);
      if (s) { setStatusPill(s.status); renderStationInfo(s); }
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
    S.clipStopAt = null;
    const hit = findChunk(ts);
    if (!hit) { toast('Nothing recorded at that time'); return; }
    if (S.mode === 'live') stopPlayback();
    loadChunk(hit.i, hit.offset, true);
  }

  // keepView: hold the waveform still while the playhead crosses into another chunk
  async function loadChunk(i, offset, autoplay, keepView) {
    const c = S.day.chunks[i];
    const prev = S.mode === 'replay' && S.day.chunks[S.chunkIdx];
    const absView = prev ? prev.start + S.viewStart : null;
    S.mode = 'replay'; S.chunkIdx = i; S.pendingSeek = offset;
    $('stMode').textContent = 'Replay'; $('stMode').className = 'pill';
    setLiveBtn(false);
    const url = `/api/streams/${S.stream.id}/audio/${S.date}/${c.file}`;
    audio.src = url + (c.in_progress ? `?t=${Date.now()}` : '');
    $('dl').href = url + '?download=1'; $('dl').hidden = false;
    $('waveLabel').textContent = `${S.date}  ${fmtTime(c.start)} – ${fmtTime(c.start + chunkLen(c))}${c.in_progress ? '  (recording)' : ''}`;
    S.peaks = chunkPeaks(c);
    [S.day.chunks[i - 1], S.day.chunks[i + 1]].forEach((n) => n && chunkPeaks(n));   // ready before you drag into them
    if (keepView && absView != null) S.viewStart = absView - c.start;
    else {
      if (!S.peaks) { $('waveMsg').textContent = 'Drawing waveform…'; $('waveMsg').hidden = false; }
      S.follow = true; S.viewStart = Math.max(0, offset - (chunkLen(c) / S.zoom) * 0.1);
    }
    setWaveZoomEnabled(true);
    if (autoplay) ensureAudioGraph();
    audio.load();
    if (autoplay) audio.play().catch(() => {});
    if (S.peaks) { $('waveMsg').hidden = true; return; }
    const p = await fetchPeaks(c);
    if (S.chunkIdx !== i || S.mode !== 'replay') return;
    if (p) { S.peaks = p; $('waveMsg').hidden = true; } else $('waveMsg').textContent = 'Waveform unavailable';
  }

  // ---- waveform peaks for any chunk, cached so neighbours can be drawn around the current one ----
  const peakCache = new Map();
  const peakKey = (c) => `${S.stream.id}|${S.date}|${c.file}|${c.in_progress ? Math.floor(c.duration / 10) : ''}`;
  function chunkPeaks(c) {            // cached peaks or null (and starts fetching them)
    const v = peakCache.get(peakKey(c));
    if (v === undefined) fetchPeaks(c);
    return v && v !== 'loading' && v !== 'error' ? v : null;
  }
  async function fetchPeaks(c) {
    const k = peakKey(c), v = peakCache.get(k);
    if (v && v !== 'loading') return v === 'error' ? null : v;
    if (v === 'loading') return peakCache.get(k + '#p');
    const pr = api(`/api/streams/${S.stream.id}/peaks/${S.date}/${c.file}`).then((p) => {
      peakCache.set(k, p); return p;
    }).catch(() => { peakCache.set(k, 'error'); return null; }).finally(() => peakCache.delete(k + '#p'));
    peakCache.set(k, 'loading'); peakCache.set(k + '#p', pr);
    if (peakCache.size > 400) peakCache.delete(peakCache.keys().next().value);   // drop the oldest
    return pr;
  }
  // chunk whose audio covers ts (prefers the one playing); null in a gap
  function chunkAt(ts) {
    const cs = S.day.chunks, cur = cs[S.chunkIdx];
    if (cur && ts >= cur.start && ts < cur.start + chunkLen(cur)) return { i: S.chunkIdx, offset: ts - cur.start };
    let lo = 0, hi = cs.length - 1, k = -1;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (cs[m].start <= ts) { k = m; lo = m + 1; } else hi = m - 1; }
    for (let j = k; j >= 0 && j >= k - 2; j--) if (ts < cs[j].start + chunkLen(cs[j])) return { i: j, offset: ts - cs[j].start };
    return null;
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
    // after a recorder restart the next chunk can begin with audio we just heard: skip it
    if (next >= 0) {
      const n = S.day.chunks[next];
      return loadChunk(next, Math.min(Math.max(0, endTs - n.start), Math.max(0, chunkLen(n) - 1)), true, true);
    }
    if (cur.in_progress) return loadChunk(S.chunkIdx, endTs - cur.start, true); // file grew, continue
    if (S.date < today()) {
      await loadDay(shiftDate(S.date, 1));
      if (S.day.chunks.length) return loadChunk(0, 0, true);
    }
    toast('Reached the end of the recordings');
  });

  function currentTs() {
    if (S.scrubTs != null) return S.scrubTs;   // timeline drag in progress: show where the pointer is
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
    setWaveZoomEnabled(false);
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
    setWaveZoomEnabled(false);
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
      // per-channel analysers for the level meter
      const split = S.actx.createChannelSplitter(2);
      src.connect(split);
      S.meterL = S.actx.createAnalyser(); S.meterR = S.actx.createAnalyser();
      S.meterL.fftSize = S.meterR.fftSize = 2048;
      split.connect(S.meterL, 0); split.connect(S.meterR, 1);
    } catch (e) { S.actx = null; }
  }

  // ---------- drawing ----------
  function frame() {
    drawWave();
    drawVU();
    updateHeads();
    requestAnimationFrame(frame);
  }

  // match the Levels panel height to the left column so their tops and bottoms line up
  (function syncMeterHeight() {
    const left = $('leftCol'), panel = $('vuPanel');
    if (!left || !panel || !window.ResizeObserver) return;
    const home = panel.parentElement;
    const place = () => {
      const narrow = window.innerWidth <= 1100;
      // on narrow screens the meter tucks under the left column instead of taking a column
      if (narrow && panel.parentElement !== left) left.appendChild(panel);
      if (!narrow && panel.parentElement !== home) home.appendChild(panel);
      panel.style.height = narrow ? '' : left.getBoundingClientRect().height + 'px';
    };
    new ResizeObserver(place).observe(left);
    window.addEventListener('resize', place);
    place();
  })();

  // ---------- level meter (dBFS, RMS bars with VU-style ballistics + peak hold) ----------
  const VU = { min: -48, max: 3, rms: [-120, -120], peak: [-120, -120], hold: [-120, -120], holdAt: [0, 0], clipAt: 0, last: 0,
    buf: new Float32Array(2048) };
  const VU_TICKS = [0, -6, -12, -20, -30, -40];
  function measure(an) {
    an.getFloatTimeDomainData(VU.buf);
    let sum = 0, pk = 0;
    for (let i = 0; i < VU.buf.length; i++) { const v = VU.buf[i]; sum += v * v; const a = Math.abs(v); if (a > pk) pk = a; }
    const db = (x) => (x > 0 ? 20 * Math.log10(x) : -120);
    return [db(Math.sqrt(sum / VU.buf.length)), db(pk), pk];
  }
  function drawVU() {
    const cv = $('vu'); if (!cv) return;
    const [g, w, h] = sizeCanvas(cv);
    const now = performance.now(), dt = Math.min(0.1, (now - (VU.last || now)) / 1000); VU.last = now;
    const active = S.meterL && (S.mode === 'replay' || S.mode === 'live') && !audio.paused;
    let ch = [[-120, -120, 0], [-120, -120, 0]];
    if (active) {
      ch = [measure(S.meterL), measure(S.meterR)];
      if (ch[1][0] < -100 && ch[0][0] > -100) ch[1] = ch[0];   // mono source: show it on both bars
    }
    const rise = 1 - Math.exp(-dt / 0.3), fall = 1 - Math.exp(-dt / 0.3);   // ~300 ms VU integration
    for (let i = 0; i < 2; i++) {
      const tgt = ch[i][0];
      VU.rms[i] += (tgt - VU.rms[i]) * (tgt > VU.rms[i] ? rise : fall);
      VU.peak[i] = Math.max(ch[i][1], VU.peak[i] - dt * 20);             // peak falls 20 dB/s
      if (ch[i][1] >= VU.hold[i] || now - VU.holdAt[i] > 1500) { VU.hold[i] = ch[i][1]; VU.holdAt[i] = now; }
      if (ch[i][2] >= 0.999) VU.clipAt = now;
    }
    g.clearRect(0, 0, w, h);
    const top = 18, bot = h - 10, span = bot - top;
    const y = (db) => top + span * (1 - (Math.max(VU.min, Math.min(VU.max, db)) - VU.min) / (VU.max - VU.min));
    // scale
    g.font = '9px ui-monospace, monospace'; g.textAlign = 'right'; g.textBaseline = 'middle';
    VU_TICKS.forEach((t) => { g.fillStyle = '#8f9bc4'; g.fillText(t === 0 ? '0' : String(t), 22, y(t)); g.fillStyle = '#27335e'; g.fillRect(26, y(t), w - 32, 1); });
    // bars
    const bw = Math.floor((w - 36) / 2) - 2, xs = [28, 28 + bw + 4];
    const grad = g.createLinearGradient(0, bot, 0, top);
    const stop = (db) => (db - VU.min) / (VU.max - VU.min);
    grad.addColorStop(0, '#1f9e8f'); grad.addColorStop(stop(-18), '#2ec4b6'); grad.addColorStop(stop(-9), '#3ddc97');
    grad.addColorStop(stop(-6), '#ffd166'); grad.addColorStop(stop(-3), '#ffb547'); grad.addColorStop(stop(0), '#ff4d5e'); grad.addColorStop(1, '#ff4d5e');
    for (let i = 0; i < 2; i++) {
      g.fillStyle = '#0b1020'; g.fillRect(xs[i], top, bw, span);
      const yr = y(VU.rms[i]);
      g.fillStyle = grad; g.fillRect(xs[i], yr, bw, bot - yr);
      // segment gaps for an LED look
      g.fillStyle = '#0b1020'; for (let yy = bot - 3; yy > top; yy -= 4) g.fillRect(xs[i], yy, bw, 1);
      // fast peak and held peak
      g.fillStyle = 'rgba(255,255,255,.55)'; g.fillRect(xs[i], y(VU.peak[i]), bw, 1);
      const hc = VU.hold[i] > -3 ? '#ff4d5e' : VU.hold[i] > -9 ? '#ffd166' : '#e8ecf8';
      if (VU.hold[i] > VU.min) { g.fillStyle = hc; g.fillRect(xs[i], y(VU.hold[i]) - 1, bw, 2); }
    }
    // L / R labels and clip light
    g.textAlign = 'center'; g.fillStyle = '#8f9bc4'; g.font = '9px system-ui, sans-serif';
    g.fillText('L', xs[0] + bw / 2, 8); g.fillText('R', xs[1] + bw / 2, 8);
    const clip = now - VU.clipAt < 1500;
    g.fillStyle = clip ? '#ff4d5e' : '#2a1820'; g.beginPath(); g.arc(12, 8, 4, 0, Math.PI * 2); g.fill();
    if (now - (VU.readAt || 0) > 200) {   // numeric readout, throttled
      VU.readAt = now;
      const f = (v) => (v <= -100 ? '—' : v.toFixed(1));
      $('vuL').textContent = `L ${active ? f(VU.rms[0]) : '—'}`;
      $('vuR').textContent = `R ${active ? f(VU.rms[1]) : '—'}`;
    }
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
    const c = S.mode === 'replay' && S.day && S.day.chunks[S.chunkIdx];
    if (!c) return;
    const dur = waveDur();
    const ct = audio.currentTime || 0;
    // keep the playhead in view while playing, unless the user panned recently
    if (!S.follow && Date.now() - S.lastPan > 4000) S.follow = true;
    const len = dur / S.zoom;
    if (S.follow && !audio.paused && (ct < S.viewStart || ct > S.viewStart + len * 0.92)) S.viewStart = ct - len * 0.08;
    clampView();
    const vs = S.viewStart, ve = vs + len;
    // one continuous strip: each bar takes its level from whichever chunk covers that moment
    const bw = S.zoom >= 8 ? 3 : 2, gap = 1, n = Math.floor(w / (bw + gap));
    for (let i = 0; i < n; i++) {
      const t0 = vs + (i / n) * len, t1 = vs + ((i + 1) / n) * len;
      const hit = chunkAt(c.start + (t0 + t1) / 2);
      if (!hit) continue;                                 // nothing recorded here
      const hc = S.day.chunks[hit.i], p = hit.i === S.chunkIdx && S.peaks ? S.peaks : chunkPeaks(hc);
      if (!p) continue;
      const pk = p.peaks, pps = p.per_second, base = c.start - hc.start;
      const a = Math.max(0, Math.floor((t0 + base) * pps)), b = Math.max(a + 1, Math.floor((t1 + base) * pps));
      let v = 0; for (let j = a; j < b && j < pk.length; j++) v = Math.max(v, pk[j]);
      const bh = Math.max(1, Math.sqrt(v) * (h - 44));
      g.fillStyle = t0 <= ct ? grad : '#3a4a80';
      g.fillRect(i * (bw + gap), mid - bh / 2, bw, bh);
    }
    const xOf = (t) => ((t - vs) / len) * w;
    // time ruler in station time
    if (c) {
      const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
      const step = steps.find((st) => (st / len) * w >= 80) || 3600;
      g.fillStyle = '#8f9bc4'; g.font = '10px ui-monospace, monospace';
      for (let t = Math.ceil((c.start + vs) / step) * step; t < c.start + ve; t += step) {
        const x = xOf(t - c.start);
        g.fillRect(x, h - 14, 1, 5);
        g.fillText(step < 60 ? fmtTime(t) : fmtHM(t), x + 3, h - 4);
      }
    }
    // song tag bands: colour block + "Artist – Title" + tags
    if (c && S.stream.is_music) {
      g.font = '600 11px system-ui, sans-serif'; g.textBaseline = 'middle';
      S.day.tracks.forEach((t, i) => {
        const r0 = t.ts - c.start, r1 = songEnd(i) - c.start;
        if (r1 < vs || r0 > ve) return;
        const x0 = Math.max(0, xOf(r0)), x1 = Math.min(w, xOf(r1));
        g.fillStyle = songColor(t, 0.22); g.fillRect(x0, 38, x1 - x0, 20);
        g.fillStyle = songColor(t, 0.95); g.fillRect(x0, 38, Math.max(2, x1 - x0), 2);
        if (r0 >= vs) { g.fillRect(xOf(r0), 38, 2, h - 56); }
        const label = `${t.artist ? t.artist + ' – ' : ''}${t.title}${t.tags && t.tags.length ? '   ' + t.tags.join(' · ') : ''}`;
        g.save(); g.beginPath(); g.rect(x0, 38, x1 - x0, 20); g.clip();
        g.fillStyle = '#e8ecf8'; g.fillText(label, x0 + 6, 49); g.restore();
      });
      g.textBaseline = 'alphabetic';
    }
    // clip in/out: shaded range plus flagged lines
    if (c && (S.markIn != null || S.markOut != null)) {
      const a = S.markIn != null ? S.markIn - c.start : null, b = S.markOut != null ? S.markOut - c.start : null;
      if (a != null && b != null && b > vs && a < ve) {
        g.fillStyle = 'rgba(255,224,102,0.14)';
        g.fillRect(Math.max(0, xOf(a)), 0, Math.min(w, xOf(b)) - Math.max(0, xOf(a)), h - 16);
      }
      g.font = '700 10px system-ui, sans-serif';
      [[a, 'IN', 0], [b, 'OUT', 1]].forEach(([r, lbl, right]) => {
        if (r == null || r < vs || r > ve) return;
        const x = xOf(r); g.fillStyle = '#ffe066'; g.fillRect(x - 1, 0, 2, h - 16);
        const tw = g.measureText(lbl).width + 8;
        g.fillRect(right ? x - tw : x, h - 32, tw, 14);
        g.fillStyle = '#0b1020'; g.fillText(lbl, (right ? x - tw : x) + 4, h - 21);
      });
    }
    if (ct >= vs && ct <= ve) { g.fillStyle = '#fff'; g.fillRect(xOf(ct), 0, 2, h - 16); }
    $('zoomLvl').textContent = (S.zoom < 10 ? S.zoom.toFixed(S.zoom % 1 ? 1 : 0) : Math.round(S.zoom)) + '×';
  }

  function setWaveZoomEnabled(on) {
    ['zoomIn', 'zoomOut', 'zoomFit'].forEach((id) => ($(id).disabled = !on));
    $('waveHelp').hidden = !on;
    if (!on) $('zoomLvl').textContent = '1×';
  }

  function waveDur() {
    if (S.peaks && S.peaks.peaks.length) return S.peaks.peaks.length / S.peaks.per_second;
    const c = S.day && S.day.chunks[S.chunkIdx];
    if (c && chunkLen(c)) return chunkLen(c);
    return isFinite(audio.duration) ? audio.duration : 1;
  }
  function maxZoom() { return Math.max(1, waveDur() / 5); } // deepest view is 5 seconds
  // the view can run on into neighbouring chunks, as far as the day's recordings go
  function clampView() {
    const len = waveDur() / S.zoom, c = S.day && S.day.chunks[S.chunkIdx];
    let lo = 0, hi = Math.max(0, waveDur() - len);
    if (c) {
      const cs = S.day.chunks, last = cs[cs.length - 1];
      lo = Math.min(lo, cs[0].start - c.start);
      hi = Math.max(hi, last.start + chunkLen(last) - c.start - len);
    }
    S.viewStart = Math.min(Math.max(lo, S.viewStart), hi);
  }
  function setZoom(z, anchorFrac = 0.5) {
    if (S.mode !== 'replay') return;
    const dur = waveDur(), oldLen = dur / S.zoom;
    const anchorT = S.viewStart + anchorFrac * oldLen;
    S.zoom = Math.min(maxZoom(), Math.max(1, z));
    S.viewStart = anchorT - anchorFrac * (dur / S.zoom);
    clampView();
  }

  function updateHeads() {
    const ts = currentTs();
    const head = $('tlHead');
    if (ts && S.day && ts >= S.day.day_start && ts < S.day.day_end) {
      const v = tlView();
      if (S.tlFollow === false && Date.now() - S.tlLastPan > 5000) S.tlFollow = true;
      if (S.tlFollow && v.span < v.full && !audio.paused && (ts < v.start || ts > v.start + v.span * 0.95)) {
        S.tlStart = ts - v.span * 0.1; renderTimelineSoon();
      }
      if (ts >= v.start && ts <= v.start + v.span) {
        head.style.display = 'block'; head.style.left = ((ts - v.start) / v.span) * 100 + '%';
      } else head.style.display = 'none';
    } else head.style.display = 'none';
    const sel = $('tlClip');
    if (S.day && S.markIn != null && S.markOut != null) {
      const v = tlView(), l = (S.markIn - v.start) / v.span, r = (S.markOut - v.start) / v.span;
      if (r < 0 || l > 1) sel.style.display = 'none';
      else {
        sel.style.display = 'block';
        sel.style.left = Math.max(0, l) * 100 + '%'; sel.style.width = (Math.min(1, r) - Math.max(0, l)) * 100 + '%';
      }
    } else sel.style.display = 'none';
    if (S.clipStopAt != null && ts != null && S.mode === 'replay' && ts >= S.clipStopAt) {
      audio.pause(); S.clipStopAt = null;
    }
    const replay = S.mode === 'replay';
    $('markIn').disabled = $('markOut').disabled = !replay;
    $('clipPlay').disabled = $('clipDl').disabled = !(S.markIn != null && S.markOut != null);
    $('clipClear').disabled = S.markIn == null && S.markOut == null;
    $('clock').textContent = ts ? fmtTime(ts) : '--:--:--';
    if (S.mode === 'replay') $('clockSub').textContent = `${S.date} · ${audio.paused ? 'paused' : 'playing'}`;
    else if (S.mode === 'live') $('clockSub').textContent = 'live · ' + (audio.paused ? 'buffering' : 'on air');
    $('playIcon').innerHTML = audio.paused ? '<path d="M8 5v14l11-7z"/>' : '<path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/>';
    if (S.stream && S.stream.is_music) updateNowPlaying(ts);
    else if (S.tr) updateTranscriptCursor(ts);
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
    $('npTags').innerHTML = t ? chipsHtml(t, false) : '';
    const art = $('npArt');
    art.style.backgroundImage = t && t.artwork_url ? `url('${t.artwork_url.replace(/'/g, '%27')}')` : '';
    art.firstElementChild.style.display = t && t.artwork_url ? 'none' : '';
    document.querySelectorAll('.trow').forEach((r) => r.classList.toggle('current', Number(r.dataset.id) === id));
  }

  // ---------- inputs ----------
  const tl = $('timeline');
  const tlFrac = (e) => { const r = tl.getBoundingClientRect(); return Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)); };
  const tsFromEvent = (e) => { const v = tlView(); return v.start + tlFrac(e) * v.span; };
  tl.addEventListener('wheel', (e) => {
    if (!S.day) return;
    e.preventDefault();
    const v = tlView();
    if (Math.abs(e.deltaX) > Math.abs(e.deltaY) || e.shiftKey) {
      S.tlStart += ((e.shiftKey ? e.deltaY : e.deltaX) / tl.clientWidth) * v.span;
    } else {
      const anchor = tlFrac(e), at = v.start + anchor * v.span;
      S.tlSpan = Math.min(v.full, Math.max(TL_MIN, v.span * Math.pow(1.0015, e.deltaY)));
      S.tlStart = at - anchor * S.tlSpan;
    }
    S.tlFollow = false; S.tlLastPan = Date.now();
    renderTimelineSoon();
  }, { passive: false });
  // ---- scrubbing: drag across the timeline or waveform and the audio follows the pointer ----
  const SEEK_MS = 50, LOAD_MS = 250;   // throttle in-chunk seeks and cross-chunk loads
  let scrub = null;
  function scrubStart(src) {
    S.clipStopAt = null;
    scrub = { src, wasPaused: S.mode === 'replay' && audio.paused, lastSeek: 0, lastLoad: 0, timer: null, ts: null };
  }
  function keepWaveInView(t) {          // keep the waveform playhead visible while scrubbing
    const len = waveDur() / S.zoom;
    if (t < S.viewStart || t > S.viewStart + len) { S.viewStart = t - len / 2; clampView(); }
  }
  function seekInChunk(t, force) {
    const now = Date.now();
    if (!force && now - scrub.lastSeek < SEEK_MS) return;
    scrub.lastSeek = now;
    if (audio.readyState < 1) S.pendingSeek = Math.max(0, t);   // chunk still loading: seek once it's ready
    else try { audio.currentTime = Math.max(0, t); } catch { /* not seekable yet */ }
    if (audio.paused) { ensureAudioGraph(); audio.play().catch(() => {}); }  // audible while dragging
  }
  // timeline scrub: may cross chunk boundaries, so loads are throttled with a trailing call
  // wave scrubs hold the waveform still and go silent over gaps; timeline scrubs jump to the next chunk
  function scrubTimelineTo(ts, final) {
    scrub.ts = ts;
    const wave = scrub.src !== 'tl';
    const hit = wave ? chunkAt(ts) : findChunk(ts);
    if (!hit) return;
    if (S.mode === 'replay' && hit.i === S.chunkIdx) {
      seekInChunk(hit.offset, final); if (!wave) keepWaveInView(hit.offset);
      return;
    }
    clearTimeout(scrub.timer);
    const wait = LOAD_MS - (Date.now() - scrub.lastLoad);
    if (final || wait <= 0) {
      scrub.lastLoad = Date.now();
      if (S.mode === 'live') stopPlayback();
      loadChunk(hit.i, hit.offset, true, wave);
    } else {
      scrub.timer = setTimeout(() => scrub && scrubTimelineTo(scrub.ts, false), wait);
    }
  }
  function scrubEnd() {
    if (!scrub) return;
    clearTimeout(scrub.timer);
    if (scrub.wasPaused) audio.pause();   // leave it paused if it was paused before the drag
    S.scrubTs = null; scrub = null;
  }

  let tlDrag = null;
  tl.addEventListener('pointerdown', (e) => {
    // left button = scrub (Shift = pan); middle button = pan
    if (!S.day || (e.button !== 0 && e.button !== 1)) return;
    const mid = e.button === 1;
    if (mid) e.preventDefault();                        // stop the browser's middle-click autoscroll
    tlDrag = { x: e.clientX, start: tlView().start, moved: false, pan: mid || e.shiftKey, mid };
    if (mid) tl.classList.add('panning');
    tl.setPointerCapture(e.pointerId);
  });
  tl.addEventListener('pointermove', (e) => {
    if (!tlDrag) return;
    const dx = e.clientX - tlDrag.x;
    if (!tlDrag.moved && Math.abs(dx) > 4) {
      tlDrag.moved = true;
      if (!tlDrag.pan) scrubStart('tl');
    }
    if (!tlDrag.moved) return;
    const v = tlView();
    if (tlDrag.pan) {                                   // Shift+drag = pan the zoomed timeline
      if (v.span < v.full) {
        tl.classList.add('panning');
        S.tlStart = tlDrag.start - (dx / tl.clientWidth) * v.span;
        S.tlFollow = false; S.tlLastPan = Date.now();
        renderTimelineSoon();
      }
      return;
    }
    tl.classList.add('dragging');                       // drag = scrub
    const ts = tsFromEvent(e);
    S.scrubTs = ts; S.tlFollow = false; S.tlLastPan = Date.now();
    scrubTimelineTo(ts, false);
  });
  const tlUp = (e) => {
    if (!tlDrag) return;
    tl.classList.remove('dragging', 'panning');
    if (tlDrag.mid) {                                   // middle-button pan never seeks
      if (tlDrag.moved) S.tlLastPan = Date.now();
    } else if (!tlDrag.moved) {
      const song = e.target.closest && e.target.closest('.song');
      playAt(song ? Number(song.dataset.ts) : tsFromEvent(e));
      S.tlFollow = true;
    } else if (scrub) {
      scrubTimelineTo(tsFromEvent(e), true);
      scrubEnd(); S.tlFollow = true;
    }
    tlDrag = null;
  };
  tl.addEventListener('pointerup', tlUp);
  // swallow middle-click side effects (autoscroll, opening links in a new tab)
  tl.addEventListener('mousedown', (e) => { if (e.button === 1) e.preventDefault(); });
  tl.addEventListener('auxclick', (e) => { if (e.button === 1) e.preventDefault(); });
  tl.addEventListener('pointercancel', tlUp);
  $('tlIn').onclick = () => { tlZoom(0.25, playheadFrac()); S.tlFollow = true; };
  $('tlOut').onclick = () => tlZoom(4, playheadFrac());
  $('tlFit').onclick = () => { tlFit(); S.tlFollow = true; };
  function playheadFrac() {
    const ts = currentTs(), v = S.day && tlView();
    if (!ts || !v) return 0.5;
    const f = (ts - v.start) / v.span; return f >= 0 && f <= 1 ? f : 0.5;
  }
  window.addEventListener('resize', () => S.day && renderTimelineSoon());
  tl.addEventListener('mousemove', (e) => {
    const hv = $('tlHover'), r = tl.getBoundingClientRect();
    hv.style.display = 'block'; hv.style.left = e.clientX - r.left + 'px';
    hv.firstElementChild.textContent = fmtHM(tsFromEvent(e));
  });
  tl.addEventListener('mouseleave', () => ($('tlHover').style.display = 'none'));

  const ww = $('waveWrap');
  const fracOf = (e) => { const r = ww.getBoundingClientRect(); return Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)); };
  ww.addEventListener('wheel', (e) => {
    if (S.mode !== 'replay') return;
    e.preventDefault();
    if (Math.abs(e.deltaX) > Math.abs(e.deltaY) || e.shiftKey) {        // pan
      const d = e.shiftKey ? e.deltaY : e.deltaX;
      S.viewStart += (d / ww.clientWidth) * (waveDur() / S.zoom); clampView();
      S.follow = false; S.lastPan = Date.now();
    } else {                                                            // zoom at cursor
      setZoom(S.zoom * Math.pow(1.0015, -e.deltaY), fracOf(e));
      S.follow = false; S.lastPan = Date.now();
    }
  }, { passive: false });
  let drag = null;
  const curChunk = () => S.day.chunks[S.chunkIdx];
  const waveTimeAt = (e) => S.viewStart + fracOf(e) * (waveDur() / S.zoom);   // relative to the playing chunk
  const waveTsAt = (e) => curChunk().start + waveTimeAt(e);                  // absolute: may be in a neighbour
  // which clip marker (if any) is under the pointer on the waveform
  function markerAt(e) {
    const c = S.day && S.day.chunks[S.chunkIdx]; if (!c) return null;
    const r = ww.getBoundingClientRect(), len = waveDur() / S.zoom;
    const near = (ts) => ts != null && Math.abs(((ts - c.start - S.viewStart) / len) * r.width - (e.clientX - r.left)) <= 6;
    return near(S.markOut) ? 'out' : near(S.markIn) ? 'in' : null;
  }
  // seek to any moment shown on the waveform, loading the neighbouring chunk if needed
  function seekWaveTo(ts) {
    const hit = chunkAt(ts);
    if (!hit) return toast('Nothing recorded at that moment');
    if (hit.i === S.chunkIdx) audio.currentTime = hit.offset;
    else loadChunk(hit.i, hit.offset, !audio.paused, true);
  }
  ww.addEventListener('pointerdown', (e) => {
    if (S.mode !== 'replay' || !curChunk() || (e.button !== 0 && e.button !== 1)) return;
    const mid = e.button === 1;
    if (mid) e.preventDefault();
    const c = curChunk();
    drag = {
      x: e.clientX, vs: S.viewStart, moved: false, mid, pan: !mid && e.shiftKey,
      marker: mid || e.shiftKey ? null : markerAt(e),
      // middle button grabs the strip: the playhead stays put and time slides under it
      absView: c.start + S.viewStart, ts0: c.start + (audio.currentTime || 0),
      spp: (waveDur() / S.zoom) / ww.clientWidth,
    };
    if (mid) ww.classList.add('panning');
    ww.setPointerCapture(e.pointerId);
  });
  ww.addEventListener('pointermove', (e) => {
    if (!drag) { ww.classList.toggle('on-marker', S.mode === 'replay' && !!markerAt(e)); return; }
    if (drag.marker) {                                  // drag an in/out flag to adjust it
      drag.moved = true;
      setMark(drag.marker, waveTsAt(e), true);
      return;
    }
    const dx = e.clientX - drag.x;
    if (!drag.moved && Math.abs(dx) > 4) {
      drag.moved = true;
      if (!drag.pan) scrubStart(drag.mid ? 'grab' : 'wave');
    }
    if (!drag.moved) return;
    S.follow = false; S.lastPan = Date.now();
    if (drag.mid) {                                     // middle-drag = grab and move through time
      const dt = dx * drag.spp;
      scrubTimelineTo(drag.ts0 - dt, false);
      S.viewStart = drag.absView - dt - curChunk().start; clampView();
      return;
    }
    if (drag.pan) {                                     // Shift+drag = pan the view only
      ww.classList.add('panning');
      S.viewStart = drag.vs - (dx / ww.clientWidth) * (waveDur() / S.zoom); clampView();
      return;
    }
    ww.classList.add('dragging');                       // drag = scrub under the pointer
    scrubTimelineTo(waveTsAt(e), false);
  });
  const waveUp = (e) => {
    if (!drag) return;
    ww.classList.remove('dragging', 'panning');
    if (drag.mid) {
      if (scrub) { scrubTimelineTo(drag.ts0 - (e.clientX - drag.x) * drag.spp, true); scrubEnd(); }
      S.lastPan = Date.now();
    } else if (drag.pan) {
      if (drag.moved) S.lastPan = Date.now();
    } else if (!drag.moved) {        // plain click = seek
      S.clipStopAt = null;
      seekWaveTo(waveTsAt(e));
      S.follow = true;
    } else if (scrub) {
      scrubTimelineTo(waveTsAt(e), true);
      scrubEnd(); S.follow = true; S.lastPan = 0;
    }
    drag = null;
  };
  ww.addEventListener('pointerup', waveUp);
  ww.addEventListener('mousedown', (e) => { if (e.button === 1) e.preventDefault(); });
  ww.addEventListener('auxclick', (e) => { if (e.button === 1) e.preventDefault(); });
  ww.addEventListener('pointercancel', waveUp);
  const zoomBtn = (f) => {          // buttons/keys zoom around the playhead and keep following it
    const len = waveDur() / S.zoom, rel = (audio.currentTime - S.viewStart) / len;
    setZoom(S.zoom * f, rel >= 0 && rel <= 1 ? rel : 0.5); S.follow = true;
  };
  $('zoomIn').onclick = (e) => { e.stopPropagation(); zoomBtn(2); };
  $('zoomOut').onclick = (e) => { e.stopPropagation(); zoomBtn(0.5); };
  $('zoomFit').onclick = (e) => { e.stopPropagation(); setZoom(1); S.follow = true; };
  ['zoomIn', 'zoomOut', 'zoomFit'].forEach((id) => $(id).addEventListener('pointerdown', (e) => e.stopPropagation()));
  const setWaveCursor = () => ww.classList.toggle('zoomed', S.zoom > 1);
  setInterval(setWaveCursor, 300);

  // ---------- transcript (talk stations) ----------
  const fmtDay = (iso) => new Date(iso + 'T12:00:00Z').toLocaleDateString('en-AU', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
  const reEsc = (x) => x.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

  async function loadTranscript(keep) {
    const date = S.date, sid = S.stream.id;
    let tr;
    try { tr = await api(`/api/streams/${sid}/transcript?date=${date}`); } catch (e) { return; }
    if (date !== S.date || sid !== S.stream.id) return;
    const prevCount = S.tr ? S.tr.segments.length : -1;
    S.tr = tr;
    renderKeywords();
    const st = tr.status || {}, done = (st.done || 0) + (st.empty || 0);
    $('trStatus').textContent = !tr.enabled ? 'Transcription is off. An admin can set it up under Admin → Transcription'
      : `${tr.segments.length} lines · ${done} chunk(s) transcribed${st.error ? ` · ${st.error} failed` : ''}${tr.queue ? ` · ${tr.queue} waiting` : ''}`;
    if ($('trQuery').value.trim()) return runSearch();              // keep showing search results
    if (keep && prevCount === tr.segments.length) return;          // nothing new: don't disturb scrolling
    renderSegments(tr.segments, false);
  }

  function markupRegex(extra) {
    // in the text: watch-list words, names/places, and search hits (topic words stay as chips only)
    const kw = S.tr ? [...S.tr.keywords.watch.map((k) => [k.term, 'watch']),
      ...S.tr.keywords.auto.filter((k) => k.kind === 'name').map((k) => [k.term, 'name'])] : [];
    const all = [...(extra || []).map((t) => [t, 'hit']), ...kw].filter(([t]) => t && t.length > 1);
    if (!all.length) return null;
    const kinds = {}; all.forEach(([t, k]) => { if (!kinds[t.toLowerCase()]) kinds[t.toLowerCase()] = k; });
    const re = new RegExp('\\b(' + Object.keys(kinds).sort((a, b) => b.length - a.length).map(reEsc).join('|') + ')\\b', 'gi');
    return { re, kinds };
  }
  function highlight(text, mk) {
    const t = esc(text);
    if (!mk) return t;
    return t.replace(mk.re, (m) => `<mark class="k-${mk.kinds[m.toLowerCase()] || 'topic'}">${m}</mark>`);
  }

  function renderSegments(segs, isSearch, terms) {
    const list = $('trList');
    if (!segs.length) {
      list.innerHTML = `<div class="empty">${isSearch ? 'No matches.' : S.tr && S.tr.enabled
        ? 'No transcript for this day yet. New audio is transcribed a few minutes after it is recorded.'
        : 'Transcription is off. An admin can turn it on under Admin → Transcription.'}</div>`;
      return;
    }
    const mk = markupRegex(terms);
    let html = '', prevEnd = null;
    for (const g of segs) {
      if (!isSearch && prevEnd !== null && g.ts - prevEnd > 300) html += `<div class="tr-gap">· · · ${Math.round((g.ts - prevEnd) / 60)} min · · ·</div>`;
      prevEnd = g.end_ts;
      html += `<div class="seg" data-ts="${g.ts}" data-end="${g.end_ts}" data-date="${g.date || ''}">
        <div class="tm">${isSearch && g.date && g.date !== S.date ? `<span class="dt">${fmtDay(g.date)}</span>` : ''}${fmtTime(g.ts)}</div>
        <div class="tx">${highlight(g.text, mk)}</div></div>`;
    }
    list.innerHTML = html;
    S.trRows = isSearch ? null : [...list.querySelectorAll('.seg')];
    S.trCur = null;
  }

  function renderKeywords() {
    const k = S.tr.keywords, chips = [...k.watch, ...k.auto];
    $('trKeywords').innerHTML = chips.map((c) => `<span class="chip ${c.kind}" data-term="${esc(c.term)}" title="${c.kind === 'watch' ? 'Watch-list word' : c.kind === 'name' ? 'Name or place' : 'Topic'}: search for it">${esc(c.term)}<span class="n">${c.count}</span></span>`).join('');
  }
  $('trKeywords').addEventListener('click', (e) => {
    const c = e.target.closest('[data-term]'); if (!c) return;
    $('trQuery').value = c.dataset.term; $('trScope').value = 'day'; runSearch();
  });

  $('trList').addEventListener('click', async (e) => {
    const row = e.target.closest('.seg'); if (!row) return;
    const ts = Number(row.dataset.ts), date = row.dataset.date;
    if (date && date !== S.date) { await loadDay(date); }
    playAt(ts);                                     // moves the timeline and waveform there
    S.follow = true; S.tlFollow = true;
    const v = tlView(); if (ts < v.start || ts > v.start + v.span) { S.tlStart = ts - v.span / 2; renderTimeline(); }
  });

  let searchTimer = null;
  $('trQuery').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 300); });
  $('trScope').addEventListener('change', runSearch);
  async function runSearch() {
    const q = $('trQuery').value.trim();
    if (!q) { S.hits = []; renderTimeline(); if (S.tr) renderSegments(S.tr.segments, false); return; }
    const scope = $('trScope').value;
    const r = await api(`/api/streams/${S.stream.id}/transcript/search?q=${encodeURIComponent(q)}${scope === 'day' ? '&date=' + S.date : ''}`).catch(() => null);
    if (!r || r.q !== q && $('trQuery').value.trim() !== q) return;
    const results = r.results.slice().sort((a, b) => a.ts - b.ts);
    S.hits = results.filter((h) => h.date === S.date);
    renderTimeline();
    const terms = (q.match(/"[^"]+"|\S+/g) || []).map((t) => t.replace(/"/g, ''));
    renderSegments(scope === 'all' ? results.reverse() : results, true, terms);
    $('trStatus').textContent = `${results.length} match${results.length === 1 ? '' : 'es'}${scope === 'all' ? ' across all days' : ' on this day'}`;
  }

  function updateTranscriptCursor(ts) {
    if (!S.trRows || !ts) return;
    // binary search for the segment containing ts
    let lo = 0, hi = S.trRows.length - 1, idx = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1, r = S.trRows[mid], st = Number(r.dataset.ts);
      if (st <= ts) { idx = mid; lo = mid + 1; } else hi = mid - 1;
    }
    const row = idx >= 0 && ts <= Number(S.trRows[idx].dataset.end) + 2 ? S.trRows[idx] : null;
    if (row === S.trCur) return;
    if (S.trCur) S.trCur.classList.remove('current');
    S.trCur = row;
    if (row) {
      row.classList.add('current');
      if ($('trFollow').checked && !audio.paused) {
        const list = $('trList');
        list.scrollTop = row.offsetTop - list.clientHeight / 3;
      }
    }
  }

  // ---------- song tags ----------
  let editingTrack = null;
  const hms = (ts) => fmtTime(ts);
  function openTrack(t) {
    editingTrack = t || null;
    const f = $('trackForm');
    $('trackDlgTitle').textContent = t ? 'Edit song tag' : 'Tag song';
    f.artist.value = t ? t.artist || '' : ''; f.title.value = t ? t.title || '' : '';
    f.album.value = t ? t.album || '' : ''; f.tags.value = t ? (t.tags || []).join(', ') : '';
    const at = t ? t.ts : currentTs();
    f.time.value = hms(at);
    $('trackDelete').hidden = !t;
    $('trackDlg').showModal();
  }
  $('tagHere').onclick = () => {
    if (S.mode !== 'replay' && S.mode !== 'live') return toast('Play something first, then tag the song at that point');
    if (S.mode === 'live' && S.date !== today()) return toast('Switch to today to tag the live stream');
    openTrack(null);
  };
  $('trackForm').addEventListener('submit', async (e) => {
    const v = e.submitter && e.submitter.value;
    if (v === 'cancel') return;
    e.preventDefault();
    const f = e.target;
    try {
      if (v === 'delete') {
        if (!confirm('Delete this song tag?')) return;
        await apiSend(`/api/tracks/${editingTrack.id}`, 'DELETE');
      } else {
        const [hh, mm, ss] = f.time.value.split(':').map(Number);
        const body = { artist: f.artist.value, title: f.title.value, album: f.album.value || null,
          tags: f.tags.value.split(',').map((x) => x.trim()).filter(Boolean),
          ts: S.day.day_start + hh * 3600 + mm * 60 + (ss || 0) };
        if (editingTrack) await apiSend(`/api/tracks/${editingTrack.id}`, 'PUT', body);
        else await apiSend(`/api/streams/${S.stream.id}/tracks`, 'POST', body);
      }
      $('trackDlg').close(); S.npTrackId = undefined;
      await loadDay(S.date, true);
      toast(v === 'delete' ? 'Song tag deleted' : 'Song tag saved');
    } catch (er) { toast(er.message, true); }
  });
  async function apiSend(path, method, body) {
    const r = await fetch(path, { method, credentials: 'same-origin', headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
    if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(Array.isArray(data.detail) ? data.detail.map((x) => x.msg).join('; ') : data.detail || r.statusText);
    return data;
  }

  // ---------- go to date and time ----------
  async function gotoDateTime(date, time) {
    const [hh, mm, ss] = time.split(':').map(Number);
    if (date !== S.date || !S.day) await loadDay(date);
    // seconds since midnight in station time; day_start is local midnight
    playAt(S.day.day_start + (hh || 0) * 3600 + (mm || 0) * 60 + (ss || 0));
  }
  $('gotoForm').addEventListener('submit', (e) => {
    e.preventDefault();
    const d = $('date').value || S.date, t = $('gotoTime').value;
    if (!t) return;
    if (d > today()) return toast('That date is in the future');
    gotoDateTime(d, t.length === 5 ? t + ':00' : t);
  });

  // ---- clip: mark an in and out point, then download that section as one MP3 ----
  const fmtLen = (sec) => {
    sec = Math.round(sec); const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), x = sec % 60;
    return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0');
  };
  function updateClipUI() {
    const info = $('clipInfo'), a = S.markIn, b = S.markOut;
    info.classList.toggle('set', a != null || b != null);
    if (a == null && b == null) info.textContent = 'Mark an in and out point on the waveform';
    else if (b == null) info.textContent = `In ${fmtTime(a)} · now set the out point`;
    else if (a == null) info.textContent = `Out ${fmtTime(b)} · now set the in point`;
    else info.textContent = `${fmtTime(a)} → ${fmtTime(b)}  (${fmtLen(b - a)})`;
  }
  function setMark(which, ts, quiet) {
    if (ts == null) return;
    if (which === 'in') S.markIn = ts; else S.markOut = ts;
    if (S.markIn != null && S.markOut != null && S.markOut < S.markIn) {   // keep in before out
      [S.markIn, S.markOut] = [S.markOut, S.markIn];
      if (drag && drag.marker) drag.marker = drag.marker === 'in' ? 'out' : 'in';
    }
    updateClipUI();
    if (!quiet && S.markIn != null && S.markOut != null && S.markOut - S.markIn < 0.5) toast('That clip is very short — move the out point later', true);
  }
  function clearMarks() { S.markIn = S.markOut = null; S.clipStopAt = null; updateClipUI(); }
  $('markIn').onclick = () => S.mode === 'replay' && setMark('in', currentTs());
  $('markOut').onclick = () => S.mode === 'replay' && setMark('out', currentTs());
  $('clipClear').onclick = clearMarks;
  $('clipPlay').onclick = () => {
    if (S.markIn == null || S.markOut == null) return;
    playAt(S.markIn); S.clipStopAt = S.markOut;
  };
  $('clipDl').onclick = async () => {
    const a = S.markIn, b = S.markOut;
    if (a == null || b == null) return;
    const btn = $('clipDl'), label = btn.lastChild.textContent;
    btn.disabled = true; btn.lastChild.textContent = 'Cutting…';
    toast(`Cutting ${fmtLen(b - a)} of audio…`);
    try {
      const r = await fetch(`/api/streams/${S.stream.id}/clip?start=${a.toFixed(3)}&end=${b.toFixed(3)}`, { credentials: 'same-origin' });
      if (r.status === 401) { location.href = '/login'; return; }
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      const name = (/filename="?([^";]+)"?/.exec(r.headers.get('Content-Disposition') || '') || [])[1] || 'clip.mp3';
      const url = URL.createObjectURL(await r.blob());
      const link = document.createElement('a'); link.href = url; link.download = name;
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      toast('Clip downloaded');
    } catch (e) { toast('Clip failed: ' + e.message, true); }
    finally { btn.lastChild.textContent = label; btn.disabled = false; }
  };
  updateClipUI();

  // ---- playback speed (replay only; live always runs at 1×) ----
  S.rate = 1;
  try { const r = Number(localStorage.getItem('rl.rate')); if ([1, 2, 5, 10].includes(r)) S.rate = r; } catch { /* storage unavailable */ }
  function applyRate() {
    const r = S.mode === 'live' ? 1 : S.rate;
    audio.preservesPitch = true;            // keep voices intelligible when sped up
    audio.defaultPlaybackRate = r;          // survives loading the next chunk
    if (audio.playbackRate !== r) audio.playbackRate = r;
    document.querySelectorAll('#speed button').forEach((b) => {
      b.classList.toggle('active', Number(b.dataset.rate) === r);
      b.disabled = S.mode === 'live';
    });
  }
  function setRate(r) {
    S.rate = r;
    try { localStorage.setItem('rl.rate', String(r)); } catch { /* storage unavailable */ }
    applyRate();
  }
  document.querySelectorAll('#speed button').forEach((b) => (b.onclick = () => setRate(Number(b.dataset.rate))));
  // a new src resets the rate in some browsers, so re-apply whenever media loads
  audio.addEventListener('loadstart', applyRate);
  audio.addEventListener('play', applyRate);
  applyRate();

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
    if (e.key === '+' || e.key === '=') zoomBtn(2);
    if (e.key === '-' || e.key === '_') zoomBtn(0.5);
    if (e.key === '0') { setZoom(1); S.follow = true; }
    if ((e.key === 'i' || e.key === 'I') && !e.ctrlKey && !e.metaKey) $('markIn').click();
    if ((e.key === 'o' || e.key === 'O') && !e.ctrlKey && !e.metaKey) $('markOut').click();
    if (e.key === 'Escape' && (S.markIn != null || S.markOut != null)) clearMarks();
  });

  boot().catch((e) => toast(e.message, true));
})();
