/* Radio Logger - 2000 outages */
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const initials = (n) => n.split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toUpperCase();
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

  let fmtDT, fmtT, fmtD, clockSkew = 0;
  function setupFormatters(tz) {
    const mk = (o) => { try { return new Intl.DateTimeFormat('en-AU', { ...o, timeZone: tz }); } catch { return new Intl.DateTimeFormat('en-AU', o); } };
    const dt = mk({ weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    const t = mk({ hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    const d = mk({ day: 'numeric', month: 'short' });
    fmtDT = (ts) => dt.format(new Date(ts * 1000));
    fmtT = (ts) => t.format(new Date(ts * 1000));
    fmtD = (ts) => d.format(new Date(ts * 1000));
  }
  const now = () => Date.now() / 1000 + clockSkew;
  function dur(sec) {
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return `${sec}s`;
    const m = Math.floor(sec / 60), s = sec % 60;
    if (m < 60) return `${m}m ${String(s).padStart(2, '0')}s`;
    const h = Math.floor(m / 60);
    if (h < 48) return `${h}h ${String(m % 60).padStart(2, '0')}m`;
    return `${Math.floor(h / 24)}d ${h % 24}h`;
  }
  const pct = (v) => v == null ? '—' : v >= 99.995 ? '100%' : v.toFixed(v >= 99 ? 2 : 1) + '%';

  // ---------- live status ----------
  const LABEL = { healthy: 'Healthy', quiet: 'Quiet', silent: 'Silent', down: 'No signal', starting: 'Connecting', disabled: 'Disabled' };
  let status = null, openIds = '';

  function stateLine(s) {
    const since = s.since ? now() - s.since : 0;
    switch (s.health) {
      case 'healthy': return 'Audio flowing';
      case 'quiet': return `Quiet for ${dur(s.quiet_for)} · outage after ${dur(status.threshold)}`;
      case 'silent': return `Silent ${dur(since)}`;
      case 'down': return `No audio ${dur(since)}`;
      case 'starting': return 'Connecting to the stream…';
      default: return 'Not being recorded';
    }
  }

  function renderHealth() {
    const list = status.streams;
    $('noStreams').hidden = list.length > 0;
    const bad = list.filter((s) => s.health === 'silent' || s.health === 'down').length;
    $('healthNote').innerHTML = `${bad ? `<b class="bad">${bad} off air</b> · ` : ''}Outage = silence or no audio for ${dur(status.threshold)}+`
      + ` · Email alarms <b>${status.alarms ? 'on' : 'off'}</b>`;
    document.title = (bad ? `(${bad}) ` : '') + 'Outages · Radio Logger - 2000';
    $('health').innerHTML = list.map((s) => {
      const off = s.health === 'silent' || s.health === 'down';
      const last = s.last_outage;
      // the detail lives in the tooltip; the tile shows only what you need at a glance
      const tip = [
        `${s.name}: ${LABEL[s.health] || s.health}, ${stateLine(s)}`,
        `Availability: 24h ${pct(s.availability.day)} · 7d ${pct(s.availability.week)}`,
        `${s.outages_24h} outage${s.outages_24h === 1 ? '' : 's'} in the last 24h`,
        !last ? 'No outages recorded' : last.end_ts
          ? `Last outage ${dur(now() - last.end_ts)} ago, lasted ${dur(last.end_ts - last.start_ts)}` : 'Outage in progress',
        s.last_error ? `Recorder: ${s.last_error}` : '',
      ].filter(Boolean).join('\n');
      const sub = off ? `<span class="htimer">${esc(stateLine(s))}</span>`
        : s.health === 'quiet' ? `Quiet ${dur(s.quiet_for)}`
        : s.health === 'healthy' || s.availability.day != null
          ? `${pct(s.availability.day)} · ${s.outages_24h ? `${s.outages_24h} today` : 'no outages'}` : esc(stateLine(s));
      return `<div class="hcard ${s.health}" data-id="${s.id}" title="${esc(tip)}">
        <div class="hlogo">${s.logo ? `<img src="${esc(s.logo)}" alt="">` : `<span class="ph">${esc(initials(s.name))}</span>`}</div>
        <div class="hbody">
          <div class="hname">${esc(s.name)}</div>
          <div class="hsub">${sub}</div>
        </div>
        <div class="hstate"><span class="hdot"></span>${LABEL[s.health] || s.health}</div>
      </div>`;
    }).join('');
  }

  async function loadStatus() {
    status = await api('/api/outages/status');
    clockSkew = status.now - Date.now() / 1000;
    renderHealth();
    // refresh the log as soon as an outage opens or closes
    const ids = status.streams.filter((s) => s.health === 'silent' || s.health === 'down').map((s) => s.id).join(',');
    if (ids !== openIds) { openIds = ids; loadLog(); }
  }

  // ---------- the log ----------
  let rows = [];
  function query() {
    return `stream_id=${$('fStream').value}&days=${$('fDays').value}`;
  }

  function renderLog() {
    const days = Number($('fDays').value);
    $('rows').innerHTML = rows.length ? rows.map((r) => `
      <tr class="${r.ongoing ? 'ongoing' : ''}">
        <td><strong>${esc(r.stream_name)}</strong></td>
        <td><span class="cause ${r.cause}">${esc(r.cause_label)}</span>${r.alerted === 1 ? ' <span class="pill" title="Alarm emailed">✉</span>' : ''}</td>
        <td class="mono">${fmtDT(r.start_ts)}</td>
        <td class="mono">${r.ongoing ? '<span class="live-txt">ongoing</span>' : (fmtD(r.end_ts) === fmtD(r.start_ts) ? fmtT(r.end_ts) : fmtDT(r.end_ts))}</td>
        <td class="mono dur" data-start="${r.start_ts}" data-end="${r.end_ts ?? ''}">${dur(r.ongoing ? now() - r.start_ts : r.duration)}</td>
        <td class="actions">${r.stream_id ? `<a class="btn sm" href="/?stream=${r.stream_id}&date=${r.replay_date}&time=${r.replay_time}" title="Replay from just before it dropped">Replay</a>` : ''}</td>
      </tr>`).join('')
      : `<tr><td colspan="6" class="empty">No outages ${days ? `in the last ${days === 1 ? '24 hours' : days + ' days'}` : 'recorded'}. 🎉</td></tr>`;

    const total = rows.reduce((a, r) => a + (r.ongoing ? now() - r.start_ts : r.duration), 0);
    const longest = rows.reduce((a, r) => Math.max(a, r.ongoing ? now() - r.start_ts : r.duration), 0);
    const silence = rows.filter((r) => r.cause === 'silence').length;
    $('summary').innerHTML = rows.length ? `
      <div><span>Outages</span><b>${rows.length}</b></div>
      <div><span>Total off air</span><b>${dur(total)}</b></div>
      <div><span>Longest</span><b>${dur(longest)}</b></div>
      <div><span>Silence / stream down</span><b>${silence} / ${rows.length - silence}</b></div>` : '';
    $('csvBtn').href = `/api/outages.csv?${query()}`;
    $('logHint').textContent = rows.length >= 1000 ? 'Showing the latest 1,000. Download the CSV for everything.' : '';
  }

  async function loadLog() {
    const r = await api(`/api/outages?${query()}`);
    rows = r.outages;
    renderLog();
  }

  // tick ongoing durations without hitting the server
  function tick() {
    document.querySelectorAll('td.dur').forEach((td) => {
      if (!td.dataset.end) td.textContent = dur(now() - Number(td.dataset.start));
    });
    if (status) document.querySelectorAll('.hcard').forEach((el) => {
      const s = status.streams.find((x) => x.id === Number(el.dataset.id));
      if (s && (s.health === 'silent' || s.health === 'down')) { const t = el.querySelector('.htimer'); if (t) t.textContent = stateLine(s); }
    });
  }

  ['fStream', 'fDays'].forEach((id) => $(id).addEventListener('change', () => {
    store.set('outages.' + id, $(id).value); loadLog().catch((e) => toast(e.message, true));
  }));
  $('health').addEventListener('click', (e) => {
    const c = e.target.closest('.hcard'); if (!c) return;
    $('fStream').value = $('fStream').value === c.dataset.id ? '0' : c.dataset.id;
    store.set('outages.fStream', $('fStream').value);
    loadLog().catch((er) => toast(er.message, true));
  });

  (async () => {
    const me = await api('/api/me');
    setupFormatters(me.tz_name);
    $('who').textContent = me.username;
    if (me.role === 'admin') $('adminLink').hidden = false;
    await loadStatus();
    $('fStream').innerHTML += status.streams.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join('');
    ['fStream', 'fDays'].forEach((id) => {
      const v = store.get('outages.' + id);
      if (v !== null && [...$(id).options].some((o) => o.value === v)) $(id).value = v;
    });
    await loadLog();
    setInterval(() => loadStatus().catch(() => {}), 5000);
    setInterval(() => loadLog().catch(() => {}), 60000);
    setInterval(tick, 1000);
  })().catch((e) => toast(e.message, true));
})();
