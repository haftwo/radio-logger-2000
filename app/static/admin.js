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
    if (b.dataset.tab === 'sso') loadSSO();
    if (b.dataset.tab === 'transcription') loadWhisper();
    if (b.dataset.tab === 'alarms') loadAlarms();
  }));

  // ---------- streams ----------
  let streams = [], editing = null, logoFor = null, me = null;
  const stateLabel = { recording: 'Recording', error: 'Error', disabled: 'Disabled', stopped: 'Stopped', starting: 'Starting' };

  function stationHtml(st) {
    if (!st || !(st.name || st.genre || st.description)) return '';
    return `<div class="hint">Stream says: ${[st.name, st.genre, st.description].filter(Boolean).map(esc).join(' · ')}</div>`;
  }

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
        <td><div><strong>${esc(s.name)}</strong></div><div class="url" title="${esc(s.url)}">${esc(s.url)}</div>
            ${stationHtml(s.station)}</td>
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
    $('detectInfo').textContent = '';
    f.enabled.checked = s ? s.enabled : true; f.is_music.checked = s ? s.is_music : false;
    f.meta_source.value = s ? s.meta_source : 'icy';
    f.transcribe.checked = s ? s.transcribe !== false : true;
    $('transcribeField').hidden = f.is_music.checked;
    $('metaField').hidden = !f.is_music.checked;
    const lg = s && s.detail.log.length;
    $('streamLog').hidden = !lg; if (lg) $('streamLog').querySelector('pre').textContent = s.detail.log.join('\n');
    $('streamDlg').showModal();
  }
  $('streamForm').is_music.addEventListener('change', (e) => { $('metaField').hidden = !e.target.checked; $('transcribeField').hidden = e.target.checked; });
  $('addStream').onclick = () => openStream(null);
  $('detectBtn').onclick = async () => {
    const f = $('streamForm'), url = f.url.value.trim();
    if (!url) return toast('Enter the stream URL first', true);
    const btn = $('detectBtn'); btn.disabled = true; btn.textContent = 'Detecting…';
    try {
      const r = await api('/api/admin/probe?url=' + encodeURIComponent(url));
      if (r.name) f.name.value = r.name;
      const bits = [r.genre, r.description, r.br && r.br + ' kbps', r.content_type].filter(Boolean);
      $('detectInfo').textContent = r.name ? `Found "${r.name}"${bits.length ? ' · ' + bits.join(' · ') : ''}`
        : (r.error ? `Couldn't read the stream: ${r.error}` : "The stream doesn't announce a name — type one in.");
      if (r.genre && /music|rock|pop|indie|jazz|dance|hits|country|classical|alternative/i.test(r.genre) && !f.is_music.checked) {
        f.is_music.checked = true; $('metaField').hidden = false;
      }
    } catch (er) { $('detectInfo').textContent = er.message; }
    finally { btn.disabled = false; btn.textContent = 'Detect from stream'; }
  };
  $('streamForm').addEventListener('submit', async (e) => {
    if (e.submitter && e.submitter.value === 'cancel') return;
    e.preventDefault();
    const f = e.target;
    const body = { name: f.name.value, url: f.url.value, enabled: f.enabled.checked, is_music: f.is_music.checked, meta_source: f.meta_source.value, transcribe: f.transcribe.checked };
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
        <td><strong>${esc(u.display_name && u.auth === 'sso' ? u.display_name : u.username)}</strong>${me && me.id === u.id ? ' <span class="pill">you</span>' : ''}
            ${u.auth === 'sso' ? `<div class="hint">${esc(u.email || u.username)}</div>` : ''}</td>
        <td>${u.auth === 'sso' ? '<span class="badge-sso">Microsoft</span>' : u.auth === 'guest' ? '<span class="pill">Public, no password</span>' : '<span class="pill">Local</span>'}</td>
        <td>${({ admin: 'Admin', guest: 'Guest' })[u.role] || 'Viewer'}</td>
        <td class="muted">${u.last_login ? new Date(u.last_login * 1000).toLocaleString() : 'never'}</td>
        <td class="actions">${u.auth === 'guest' ? '' : `<button class="btn" data-uedit="${u.id}">Edit</button>`}
          ${me && me.id === u.id ? '' : `<button class="btn danger" data-udel="${u.id}">Delete</button>`}</td>
      </tr>`).join('');
  }
  $('userRows').addEventListener('click', async (e) => {
    const t = e.target.closest('[data-uedit],[data-udel]'); if (!t) return;
    if (t.dataset.uedit) return openUser(users.find((u) => u.id === Number(t.dataset.uedit)));
    const u = users.find((x) => x.id === Number(t.dataset.udel));
    if (!confirm(`Delete user "${u.username}"?`)) return;
    await api(`/api/admin/users/${u.id}`, 'DELETE').then(() => toast('User deleted')).catch((er) => toast(er.message, true));
    loadUsers(); loadGuest();
  });
  function openUser(u) {
    editingUser = u || null;
    const f = $('userForm');
    $('userDlgTitle').textContent = u ? `Edit ${u.username}` : 'Add user';
    $('pwLabel').textContent = u ? 'New password (leave blank to keep)' : 'Password (8+ characters)';
    f.username.value = u ? u.username : ''; f.password.value = ''; f.role.value = u ? u.role : 'viewer';
    f.password.required = !u;
    const isSSO = u && u.auth === 'sso';
    f.username.readOnly = !!isSSO;
    f.password.closest('label').hidden = !!isSSO;
    $('ssoUserNote').hidden = !isSSO;
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
      $('userDlg').close(); toast(editingUser && editingUser.auth === 'sso' ? 'Saved. Microsoft re-syncs the role at next sign-in' : 'User saved'); loadUsers();
    } catch (er) { toast(er.message, true); }
  });

  // ---------- public guest access ----------
  async function loadGuest() {
    const g = await api('/api/admin/guest'), f = $('guestFormAdmin');
    f.enabled.checked = g.enabled; f.live_limit.value = g.live_limit;
    $('guestNow').textContent = g.enabled ? `Guests listening live now: ${g.live_now}.` : '';
  }
  $('guestFormAdmin').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = e.target;
    try {
      await api('/api/admin/guest', 'PUT', { enabled: f.enabled.checked, live_limit: Number(f.live_limit.value) });
      toast(f.enabled.checked ? 'Guest access is on' : 'Guest access is off; guests signed out');
      loadUsers(); loadGuest();
    } catch (er) { toast(er.message, true); }
  });

  // ---------- transcription ----------
  const W_KEYS = ['whisper_api', 'whisper_url', 'whisper_model', 'whisper_language', 'whisper_backfill_hours', 'whisper_keywords', 'whisper_timeout'];
  const ago = (t) => { if (!t) return ''; const s = Math.round(Date.now() / 1000 - t); return s < 90 ? `${s}s ago` : `${Math.round(s / 60)} min ago`; };
  function whisperApiChanged() {
    const f = $('whisperForm'), api = f.whisper_api.value;
    $('modelField').hidden = api !== 'openai';
    $('keyField').hidden = api === 'wyoming';
    f.whisper_url.placeholder = { openai: 'http://192.168.1.50:8000', asr: 'http://192.168.1.50:9000', wyoming: 'tcp://192.168.1.50:10300' }[api];
    $('urlHint').textContent = { openai: 'Base address of the server. /v1/audio/transcriptions is added automatically.',
      asr: 'Base address of the server. /asr is added automatically.',
      wyoming: 'tcp://host:port of the Wyoming server.' }[api];
  }
  async function loadWhisper(statusOnly) {
    const c = await api('/api/admin/transcription'), f = $('whisperForm');
    if (!statusOnly) {
      f.whisper_enabled.checked = c.whisper_enabled;
      W_KEYS.forEach((k) => (f[k].value = c[k] ?? ''));
      f.whisper_api_key.value = '';
      $('keyState').textContent = c.key_set ? '· saved' : '';
      whisperApiChanged();
    }
    const st = c.state;
    $('wBusy').textContent = st.busy || (c.whisper_enabled ? 'idle' : 'off');
    $('wQueue').textContent = c.whisper_enabled ? `${st.queue} chunk(s)` : '—';
    $('wDone').textContent = `${(c.counts.done || 0) + (c.counts.empty || 0)} · ${c.segments} lines`;
    $('wSecs').textContent = st.last_seconds ? `${st.last_seconds}s ${st.last_ok_at ? '(' + ago(st.last_ok_at) + ')' : ''}` : '—';
    $('wFail').textContent = c.counts.error || 0;
    $('wErr').textContent = st.last_error ? `Last error ${ago(st.last_error_at)}: ${st.last_error}` : '';
    $('whisperRetry').hidden = !c.counts.error;
    $('wTalk').textContent = c.talk_streams.length ? `Transcribing: ${c.talk_streams.join(', ')}` : 'No talk stations are set to transcribe yet. Untick "Music station" on a stream.';
  }
  function whisperBody() {
    const f = $('whisperForm'), b = { whisper_enabled: f.whisper_enabled.checked, whisper_api_key: f.whisper_api_key.value.trim() || null };
    W_KEYS.forEach((k) => (b[k] = f[k].value.trim()));
    b.whisper_backfill_hours = Number(b.whisper_backfill_hours) || 24; b.whisper_timeout = Number(b.whisper_timeout) || 900;
    return b;
  }
  $('whisperForm').whisper_api.addEventListener('change', whisperApiChanged);
  $('whisperForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    try { await api('/api/admin/transcription', 'PUT', whisperBody()); toast('Transcription settings saved'); loadWhisper(); }
    catch (er) { toast(er.message, true); }
  });
  $('whisperTest').onclick = async () => {
    const out = $('whisperTestResult'), btn = $('whisperTest');
    btn.disabled = true; btn.textContent = 'Testing…'; out.textContent = ''; out.style.color = '';
    try {
      const r = await api('/api/admin/transcription/test', 'POST', whisperBody());
      out.textContent = `✓ Server answered in ${r.seconds}s. ${r.text}`; out.style.color = 'var(--ok)';
    } catch (er) { out.textContent = er.message; out.style.color = 'var(--live)'; }
    finally { btn.disabled = false; btn.textContent = 'Test connection'; }
  };
  $('whisperRetry').onclick = async () => { await api('/api/admin/transcription/retry', 'POST'); toast('Failed chunks will be retried'); loadWhisper(true); };
  setInterval(() => { if ($('tab-transcription').classList.contains('active')) loadWhisper(true); }, 5000);

  // ---------- outage detection and email alarms ----------
  const A_KEYS = ['outage_threshold_seconds', 'silence_db', 'alarm_recipients', 'alarm_delay_seconds', 'smtp_host', 'smtp_port', 'smtp_security', 'smtp_username', 'smtp_from'];
  async function loadAlarms(statusOnly) {
    const c = await api('/api/admin/alarms'), f = $('alarmForm');
    if (!statusOnly) {
      A_KEYS.forEach((k) => (f[k].value = c[k] ?? ''));
      f.alarm_enabled.checked = c.alarm_enabled; f.alarm_on_recovery.checked = c.alarm_on_recovery;
      f.smtp_password.value = '';
      $('smtpPwState').textContent = c.password_set ? '· saved' : '';
      $('smtpPwClear').hidden = !c.password_set;
    }
    const on = c.alarm_enabled && c.smtp_host && c.alarm_recipients;
    $('aState').textContent = on ? 'On' : c.alarm_enabled ? 'Incomplete settings' : 'Off';
    $('aState').style.color = on ? 'var(--ok)' : c.alarm_enabled ? 'var(--warn)' : '';
    $('aSent').textContent = c.state.last_sent_at ? new Date(c.state.last_sent_at * 1000).toLocaleString() : 'never';
    $('aErr').textContent = c.state.last_error ? `Last failure ${ago(c.state.last_error_at)}: ${c.state.last_error}` : '';
    $('aLinks').textContent = c.public_url ? `Emails link to ${c.public_url}.`
      : 'Tip: set the Public URL under Single sign-on and alarm emails will include a link to replay the moment the audio dropped.';
  }
  function alarmBody() {
    const f = $('alarmForm'), b = {};
    A_KEYS.forEach((k) => (b[k] = f[k].value.trim()));
    ['outage_threshold_seconds', 'silence_db', 'alarm_delay_seconds', 'smtp_port'].forEach((k) => (b[k] = Number(b[k])));
    b.alarm_enabled = f.alarm_enabled.checked; b.alarm_on_recovery = f.alarm_on_recovery.checked;
    b.smtp_password = f.smtp_password.value || null;
    return b;
  }
  $('alarmForm').smtp_security.addEventListener('change', (e) => {
    const port = $('alarmForm').smtp_port;
    if (['587', '465', '25', ''].includes(port.value)) port.value = { starttls: 587, ssl: 465, none: 25 }[e.target.value];
  });
  $('alarmForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      const r = await api('/api/admin/alarms', 'PUT', alarmBody());
      toast(r.restarted ? 'Saved. Recorders restarted with the new silence level' : 'Alarm settings saved'); loadAlarms();
    } catch (er) { toast(er.message, true); }
  });
  $('alarmTest').onclick = async () => {
    const out = $('alarmTestResult'), btn = $('alarmTest');
    btn.disabled = true; btn.textContent = 'Sending…'; out.textContent = ''; out.style.color = '';
    try {
      const r = await api('/api/admin/alarms/test', 'POST', alarmBody());
      out.textContent = `✓ Sent to ${r.to.join(', ')}. Check the inbox (and spam folder).`; out.style.color = 'var(--ok)';
    } catch (er) { out.textContent = er.message; out.style.color = 'var(--live)'; }
    finally { btn.disabled = false; btn.textContent = 'Send test email'; }
  };
  $('smtpPwClear').onclick = async () => {
    if (!confirm('Forget the saved SMTP password?')) return;
    await api('/api/admin/alarms/password', 'DELETE'); toast('Password removed'); loadAlarms();
  };
  setInterval(() => { if ($('tab-alarms').classList.contains('active')) loadAlarms(true); }, 10000);

  // ---------- single sign-on ----------
  async function loadSSO() {
    const c = await api('/api/admin/sso'), f = $('ssoForm');
    f.sso_enabled.checked = c.sso_enabled;
    ['sso_tenant_id', 'sso_client_id', 'sso_admin_role', 'sso_admin_group', 'sso_viewer_role', 'sso_public_url', 'sso_button_label']
      .forEach((k) => (f[k].value = c[k] || ''));
    f.sso_client_secret.value = '';
    f.sso_password_login.value = c.sso_password_login;
    f.querySelectorAll('[name=sso_viewer_access]').forEach((r) => (r.checked = r.value === c.sso_viewer_access));
    $('secretState').textContent = c.secret_set ? '· saved' : '· not set';
    $('redirectUri').value = c.redirect_uri;
    $('localAdmins').textContent = `${c.local_admins} local admin account(s) with a password, used for break-glass access if Microsoft sign-in is unavailable.`;
    const st = $('ssoStatus');
    st.textContent = c.ready ? 'On: Microsoft sign-in active' : c.sso_enabled ? 'Incomplete settings' : 'Off';
    st.style.color = c.ready ? 'var(--ok)' : c.sso_enabled ? 'var(--warn)' : '';
    $('ssoTest').hidden = !c.ready;
    if (!c.redirect_uri.startsWith('https://')) {
      $('redirectUri').title = 'Microsoft only accepts https:// redirect URIs (except localhost). Set the Public URL.';
      st.textContent += ' · redirect URI is not HTTPS';
    }
  }
  $('copyRedirect').onclick = async () => {
    try { await navigator.clipboard.writeText($('redirectUri').value); toast('Redirect URI copied'); }
    catch { $('redirectUri').select(); toast('Press Ctrl/Cmd+C to copy'); }
  };
  $('ssoForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = e.target, v = (k) => f[k].value.trim();
    const body = {
      sso_enabled: f.sso_enabled.checked, sso_tenant_id: v('sso_tenant_id'), sso_client_id: v('sso_client_id'),
      sso_client_secret: f.sso_client_secret.value.trim() || null, sso_admin_role: v('sso_admin_role'),
      sso_admin_group: v('sso_admin_group'), sso_viewer_role: v('sso_viewer_role'),
      sso_viewer_access: (f.querySelector('[name=sso_viewer_access]:checked') || {}).value || 'tenant',
      sso_password_login: f.sso_password_login.value, sso_public_url: v('sso_public_url'),
      sso_button_label: v('sso_button_label') || 'Sign in with Microsoft',
    };
    try { await api('/api/admin/sso', 'PUT', body); toast('Single sign-on saved'); loadSSO(); }
    catch (er) { toast(er.message, true); }
  });

  (async () => {
    me = await api('/api/me');
    $('who').textContent = me.username;
    await loadStreams(); await loadUsers(); loadGuest();
    setInterval(() => { if ($('tab-streams').classList.contains('active') && !$('streamDlg').open) loadStreams(); }, 10000);
  })().catch((e) => toast(e.message, true));
})();
