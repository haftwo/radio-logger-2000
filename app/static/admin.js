/* Radio Logger - 2000 admin */
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const initials = (n) => n.split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toUpperCase();
  const gb = (b) => b >= 1e12 ? (b / 1e12).toFixed(1) + ' TB' : (b / 1e9).toFixed(b > 1e11 ? 0 : 1) + ' GB';

  async function api(path, method = 'GET', body) {
    const opts = { method, credentials: 'same-origin', headers: {} };
    if (body instanceof FormData) opts.body = body;
    else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers['Content-Type'] = 'application/json'; }
    const r = await fetch(path, opts);
    if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const d = data.detail;
      throw new Error(Array.isArray(d) ? d.map((x) => `${x.loc.slice(-1)[0]}: ${x.msg}`).join('; ') : d || r.statusText);
    }
    return data;
  }
  function toast(msg, err) {
    const t = $('toast'); t.textContent = msg; t.className = 'toast show' + (err ? ' err' : '');
    clearTimeout(toast.h); toast.h = setTimeout(() => (t.className = 'toast'), 4000);
  }

  // tabs
  document.querySelectorAll('.tabs button').forEach((b) => b.addEventListener('click', () => {
    document.querySelectorAll('.tabs button').forEach((x) => x.classList.toggle('active', x === b));
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.id === 'tab-' + b.dataset.tab));
    if (b.dataset.tab === 'recording') loadSettings();
  }));

  // ---------- streams ----------
  let streams = [], editing = null, logoFor = null, me = null;
  const stateLabel = { recording: 'Recording', error: 'Error', disabled: 'Disabled', stopped: 'Stopped', starting: 'Starting' };

  function detectHtml(d) {
    if (!d) return '';
    const cls = { ok: 'recording', error: 'error' }[d.state] || '';
    return `<div class="hint" title="${esc(d.message)}"><span class="pill ${cls === 'recording' ? '' : cls}" style="margin-right:6px">${esc(d.source)}</span>${esc(d.message).slice(0, 90)}</div>`;
  }

  async function loadStreams() {
    streams = await api('/api/admin/streams');
    $('streamRows').innerHTML = streams.length ? streams.map((s) => `
      <tr>
        <td><div class="slogo" data-logo="${s.id}" title="Upload logo">${s.logo ? `<img src="${esc(s.logo)}" alt="">` : `<span class="ph">${esc(initials(s.name))}</span>`}</div></td>
        <td><div><strong>${esc(s.name)}</strong></div><div class="url" title="${esc(s.url)}">${esc(s.url)}</div></td>
        <td><span class="pill ${s.status}" title="${esc(s.detail.last_error)}"><span class="dot"></span>${stateLabel[s.status] || s.status}</span>
            ${s.detail.restarts ? `<div class="hint">${s.detail.restarts} restart(s)</div>` : ''}</td>
        <td>${s.is_music ? `Music · ${s.meta_source === 'icy' ? 'auto' : s.meta_source === 'shazam' ? 'Shazam' : 'no detection'}` : 'Talk / general'}
            ${detectHtml(s.detail.detect)}</td>
        <td class="actions">
          <button class="btn" data-restart="${s.id}" title="Restart recorder">Restart</button>
          <button class="btn" data-edit="${s.id}">Edit</button>
          <button class="btn danger" data-del="${s.id}">Delete</button>
        </td>
      </tr>`).join('') : '<tr><td colspan="5" class="empty">No streams yet. Add your first station.</td></tr>';
  }

  $('streamRows').addEventListener('click', async (e) => {
    const t = e.target.closest('[data-edit],[data-del],[data-restart],[data-logo]');
    if (!t) return;
    if (t.dataset.logo) { logoFor = Number(t.dataset.logo); $('logoInput').click(); return; }
    if (t.dataset.edit) return openStream(streams.find((s) => s.id === Number(t.dataset.edit)));
    if (t.dataset.restart) {
      await api(`/api/admin/streams/${t.dataset.restart}/restart`, 'POST').then(() => toast('Recorder restarted')).catch((er) => toast(er.message, true));
      return setTimeout(loadStreams, 1500);
    }
    if (t.dataset.del) {
      const s = streams.find((x) => x.id === Number(t.dataset.del));
      if (!confirm(`Delete "${s.name}"? Recording stops; files already on disk are kept.`)) return;
      await api(`/api/admin/streams/${s.id}`, 'DELETE').then(() => toast('Stream deleted')).catch((er) => toast(er.message, true));
      loadStreams();
    }
  });

  $('logoInput').addEventListener('change', async (e) => {
    const f = e.target.files[0]; e.target.value = '';
    if (!f || !logoFor) return;
    const fd = new FormData(); fd.append('file', f);
    try { await api(`/api/admin/streams/${logoFor}/logo`, 'POST', fd); toast('Logo updated'); loadStreams(); }
    catch (er) { toast(er.message, true); }
  });

  function openStream(s) {
    editing = s || null;
    const f = $('streamForm');
    $('streamDlgTitle').textContent = s ? `Edit ${s.name}` : 'Add stream';
    f.name.value = s ? s.name : ''; f.url.value = s ? s.url : '';
    f.enabled.checked = s ? s.enabled : true; f.is_music.checked = s ? s.is_music : false;
    f.meta_source.value = s ? s.meta_source : 'icy';
    $('metaField').hidden = !f.is_music.checked;
    const lg = s && s.detail.log.length;
    $('streamLog').hidden = !lg; if (lg) $('streamLog').querySelector('pre').textContent = s.detail.log.join('\n');
    $('streamDlg').showModal();
  }
  $('streamForm').is_music.addEventListener('change', (e) => ($('metaField').hidden = !e.target.checked));
  $('addStream').onclick = () => openStream(null);
  $('streamForm').addEventListener('submit', async (e) => {
    if (e.submitter && e.submitter.value === 'cancel') return;
    e.preventDefault();
    const f = e.target;
    const body = { name: f.name.value, url: f.url.value, enabled: f.enabled.checked, is_music: f.is_music.checked, meta_source: f.meta_source.value };
    try {
      if (editing) await api(`/api/admin/streams/${editing.id}`, 'PUT', body);
      else await api('/api/admin/streams', 'POST', body);
      $('streamDlg').close(); toast(editing ? 'Stream updated' : 'Stream added — recording starts now');
      setTimeout(loadStreams, 800);
    } catch (er) { toast(er.message, true); }
  });

  // ---------- settings ----------
  async function loadSettings() {
    const s = await api('/api/admin/settings');
    const f = $('settingsForm');
    ['recordings_path', 'retention_days', 'chunk_minutes', 'bitrate_kbps', 'shazam_interval'].forEach((k) => (f[k].value = s[k]));
    const d = s.disk;
    $('dUsed').textContent = gb(d.logger); $('dFree').textContent = gb(d.free); $('dTotal').textContent = gb(d.total);
    $('dBar').style.width = d.total ? Math.min(100, ((d.total - d.free) / d.total) * 100) + '%' : '0';
    const perDay = (Number(s.bitrate_kbps) * 1000 / 8) * 86400;
    const n = streams.filter((x) => x.enabled).length;
    $('dEst').textContent = `At ${s.bitrate_kbps} kbps each stream uses about ${gb(perDay)} per day — ${n} active stream(s) × ${s.retention_days} days ≈ ${gb(perDay * n * Number(s.retention_days))}.`;
  }
  $('settingsForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = e.target;
    const body = { recordings_path: f.recordings_path.value, retention_days: Number(f.retention_days.value),
      chunk_minutes: Number(f.chunk_minutes.value), bitrate_kbps: Number(f.bitrate_kbps.value), shazam_interval: Number(f.shazam_interval.value) };
    try {
      const r = await api('/api/admin/settings', 'PUT', body);
      toast(r.restarted ? 'Saved — recorders restarted with the new settings' : 'Settings saved');
      loadSettings();
    } catch (er) { toast(er.message, true); }
  });

  // ---------- users ----------
  let users = [], editingUser = null;
  async function loadUsers() {
    users = await api('/api/admin/users');
    $('userRows').innerHTML = users.map((u) => `
      <tr>
        <td><strong>${esc(u.username)}</strong>${me && me.id === u.id ? ' <span class="pill">you</span>' : ''}</td>
        <td>${u.role === 'admin' ? 'Admin' : 'Viewer'}</td>
        <td class="muted">${new Date(u.created_at * 1000).toLocaleDateString()}</td>
        <td class="actions"><button class="btn" data-uedit="${u.id}">Edit</button>
          ${me && me.id === u.id ? '' : `<button class="btn danger" data-udel="${u.id}">Delete</button>`}</td>
      </tr>`).join('');
  }
  $('userRows').addEventListener('click', async (e) => {
    const t = e.target.closest('[data-uedit],[data-udel]'); if (!t) return;
    if (t.dataset.uedit) return openUser(users.find((u) => u.id === Number(t.dataset.uedit)));
    const u = users.find((x) => x.id === Number(t.dataset.udel));
    if (!confirm(`Delete user "${u.username}"?`)) return;
    await api(`/api/admin/users/${u.id}`, 'DELETE').then(() => toast('User deleted')).catch((er) => toast(er.message, true));
    loadUsers();
  });
  function openUser(u) {
    editingUser = u || null;
    const f = $('userForm');
    $('userDlgTitle').textContent = u ? `Edit ${u.username}` : 'Add user';
    $('pwLabel').textContent = u ? 'New password (leave blank to keep)' : 'Password (8+ characters)';
    f.username.value = u ? u.username : ''; f.password.value = ''; f.role.value = u ? u.role : 'viewer';
    f.password.required = !u;
    $('userDlg').showModal();
  }
  $('addUser').onclick = () => openUser(null);
  $('userForm').addEventListener('submit', async (e) => {
    if (e.submitter && e.submitter.value === 'cancel') return;
    e.preventDefault();
    const f = e.target;
    const body = { username: f.username.value, password: f.password.value || null, role: f.role.value };
    try {
      if (editingUser) await api(`/api/admin/users/${editingUser.id}`, 'PUT', body);
      else await api('/api/admin/users', 'POST', body);
      $('userDlg').close(); toast('User saved'); loadUsers();
    } catch (er) { toast(er.message, true); }
  });

  (async () => {
    me = await api('/api/me');
    $('who').textContent = me.username;
    await loadStreams(); await loadUsers();
    setInterval(() => { if ($('tab-streams').classList.contains('active') && !$('streamDlg').open) loadStreams(); }, 10000);
  })().catch((e) => toast(e.message, true));
})();
