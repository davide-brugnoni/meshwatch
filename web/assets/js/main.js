// MeshWatch — avvio della pagina
// ---------- avvio
async function refresh() {
  try { state = await api('/api/state?hours=' + $('range').value); render(); } catch (e) { }
  if (state && state.unread != null) setUnread(state.unread);
  loadNetwork();
}
async function init() {
  try { me = await api('/api/me'); } catch (e) { return; }
  if (!me.role) { showLogin(); return; }   // mappa non libera: serve il login
  const guest = me.role === 'guest';
  document.body.classList.toggle('admin', me.role === 'admin');
  document.body.classList.toggle('guest', guest);
  $('logout').style.display = guest ? 'none' : '';
  $('login-btn').style.display = guest ? '' : 'none';
  $('login-btn').textContent = me.setup ? 'Imposta password' : 'Accedi';
  $('whoami').textContent = guest ? '' : me.username;
  if (!guest) {
    layersCtl.addOverlay(netLayer, 'Rete MeshCore');
    let on = false; try { on = localStorage.getItem('netLayer') === '1'; } catch (e) { }
    if (on) netLayer.addTo(map);
  }
  await refresh();
  if (me.role === 'admin' && location.hash === '#gestione') setTab('admin');
  else if (location.hash === '#chat' && !guest) setTab('chat');
  else if (location.hash === '#nodi' && isPhone()) setTab('list');
  chatPollSoon();
  setInterval(refresh, 60000);
  if (me.role === 'admin') updateBadge();
  // sul telefono, quando si torna alla pagina (app in primo piano), aggiorna subito
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { refresh(); chatPollSoon(); } });
}
init();
