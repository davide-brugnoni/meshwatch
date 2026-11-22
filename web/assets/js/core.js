// MeshWatch — utilità, chiamate all'API, login e schede
const $ = id => document.getElementById(id);
const el = (tag, props = {}, ...kids) => { const e = document.createElement(tag); Object.assign(e, props); e.append(...kids.filter(k => k != null)); return e; };
let me = null, state = null, adm = null, fitDone = false;

// ---------- utilità
function toast(msg, err) { const t = $('toast'); t.textContent = msg; t.className = err ? 'err' : ''; t.style.display = 'block'; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = 'none', 3500); }
function fmtAgo(ts) {
  const min = Math.round((Date.now() / 1000 - ts) / 60);
  return min < 1 ? 'adesso' : min < 120 ? `${min} min fa` : min < 2880 ? `${Math.round(min / 60)} ore fa` : `${Math.round(min / 1440)} giorni fa`;
}
function fmtTs(ts) {
  if (!ts) return 'mai';
  const d = new Date(ts * 1000);
  return `${d.toLocaleDateString('it-IT', { day: '2-digit', month: '2-digit' })} ${d.toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' })} · ${fmtAgo(ts)}`;
}
function fmtUp(s) { if (!s) return '—'; const d = Math.floor(s / 86400); return `${d}g ${Math.floor((s % 86400) / 3600)}h`; }
const mapsUrl = (lat, lon) => `https://www.google.com/maps?q=${lat.toFixed(6)},${lon.toFixed(6)}`;
const dot = cls => el('span', { className: 'dot ' + (cls || '') });
const swatch = c => { const s = el('span', { className: 'swatch' }); s.style.background = c; return s; };

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, ...opts });
  if (r.status === 401) { showLogin(); throw new Error('login'); }
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    const d = Array.isArray(j.detail) ? 'valori non validi' : j.detail;
    throw new Error(d || r.statusText);
  }
  return r.json();
}
const post = (p, b) => api(p, { method: 'POST', body: JSON.stringify(b || {}) });
const put = (p, b) => api(p, { method: 'PUT', body: JSON.stringify(b || {}) });
const del = p => api(p, { method: 'DELETE' });
async function action(btn, fn, okMsg) {
  if (btn) btn.disabled = true;
  try { const r = await fn(); if (okMsg) toast(typeof okMsg === 'function' ? okMsg(r) : okMsg); await refresh(); if (adm) await loadAdmin(); return r; }
  catch (e) { if (e.message !== 'login') toast('Errore: ' + e.message, true); }
  finally { if (btn) btn.disabled = false; }
}

// ---------- login
// la mappa può essere libera: in quel caso il login si può chiudere
function showLogin() {
  const setup = !!(me && me.setup), canClose = !!(me && me.role);
  $('login-form').hidden = setup; $('setup-form').hidden = !setup;
  $('login-msg').hidden = !canClose;
  document.querySelectorAll('.login-cancel').forEach(b => b.hidden = !canClose);
  $('login').style.display = 'flex';
  const f = setup ? $('setup-form') : $('login-form');
  setTimeout(() => f.querySelector(setup ? 'input[type=password]' : 'input').focus(), 50);
}
function hideLogin() { $('login').style.display = 'none'; if (document.body.classList.contains('tab-admin')) setTab('map'); }
document.querySelectorAll('.login-cancel').forEach(b => b.onclick = hideLogin);
$('login-btn').onclick = showLogin;
$('setup-form').onsubmit = async e => {
  e.preventDefault();
  const f = e.target, err = $('setup-err');
  if (f.password.value !== f.password2.value) { err.textContent = 'Le due password non coincidono'; return; }
  const r = await fetch('/api/setup', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: f.username.value.trim(), password: f.password.value }) });
  if (!r.ok) { const j = await r.json().catch(() => ({})); err.textContent = Array.isArray(j.detail) ? 'Utente (2-32 lettere/numeri) o password (min 8) non validi' : (j.detail || 'Errore'); return; }
  location.hash = '#gestione'; location.reload();
};
$('login-form').onsubmit = async e => {
  e.preventDefault();
  const f = new FormData(e.target);
  const r = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: f.get('username'), password: f.get('password') }) });
  if (!r.ok) { const j = await r.json().catch(() => ({})); $('login-err').textContent = r.status === 429 ? j.detail : 'Credenziali errate'; return; }
  location.reload();
};
$('logout').onclick = async () => { await fetch('/api/logout', { method: 'POST' }); location.reload(); };

// ---------- schede
// 'map' e 'list' su computer sono la stessa vista (mappa + colonna); sul telefono sono separate
function setTab(t) {
  if (t === 'admin' && (!me || me.role !== 'admin')) { showLogin(); return; }
  if (t === 'chat' && (!me || !['admin', 'viewer'].includes(me.role))) { showLogin(); return; }
  history.replaceState(null, '', t === 'admin' ? '#gestione' : t === 'list' ? '#nodi' : t === 'chat' ? '#chat' : '#');
  document.body.classList.toggle('tab-admin', t === 'admin');
  document.body.classList.toggle('tab-list', t === 'list');
  document.body.classList.toggle('tab-chat', t === 'chat');
  $('tab-map').classList.toggle('on', t === 'map' || t === 'list'); $('tab-admin').classList.toggle('on', t === 'admin');
  $('tab-chat').classList.toggle('on', t === 'chat');
  document.querySelectorAll('#mnav button').forEach(b => b.classList.toggle('on', b.dataset.t === t));
  if (t === 'admin') { loadAdmin(); loadGateway(); loadSystem(); loadUpdate(); loadMaintenance(); loadBatteryAlerts(); loadTileCache(); loadContacts(); loadReport(); loadRestore(); }
  else if (t === 'chat') loadChat();
  else setTimeout(() => map.invalidateSize(), 50);
  chatPollSoon();
}
$('tab-map').onclick = () => setTab('map');
$('tab-chat').onclick = () => setTab('chat');
$('tab-admin').onclick = () => setTab('admin');
document.querySelectorAll('#mnav button').forEach(b => b.onclick = () => setTab(b.dataset.t));
function focusTracker(t) {
  const p = t.last_position; if (!p) return;
  if (isPhone()) setTab('map');
  map.setView([p.lat, p.lon], Math.max(map.getZoom(), 15));
  const m = lastMarkers[t.id]; if (m) setTimeout(() => m.openPopup(), 80);
}
