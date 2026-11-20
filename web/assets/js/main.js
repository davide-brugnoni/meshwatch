// MeshWatch — avvio della pagina
// ---------- avvio
let lastRefresh = 0;
async function refresh() {
  // chi sta scrivendo nella colonna (es. l'intervallo) non deve vedersi cancellare il campo
  const a = document.activeElement;
  if (a && a.tagName === 'INPUT' && ($('trackers').contains(a) || $('reps').contains(a))) { setTimeout(refresh, 5000); return; }
  lastRefresh = Date.now();
  try { state = await api('/api/state?hours=' + $('range').value); render(); } catch (e) { }
  if (state && state.unread != null) setUnread(state.unread);
  loadNetwork();
  loadCoverage();
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
    layersCtl.addOverlay(covLayer, 'Copertura');
    let cOn = false; try { cOn = localStorage.getItem('covLayer') === '1'; } catch (e) { }
    if (cOn) covLayer.addTo(map);
    layersCtl.addOverlay(routeLayer, 'Percorsi');
    let rOn = true; try { rOn = localStorage.getItem('routeLayer') !== '0'; } catch (e) { }
    if (rOn) routeLayer.addTo(map);
  }
  await refresh();
  if (me.role === 'admin' && location.hash === '#gestione') setTab('admin');
  else if (location.hash === '#chat' && !guest) setTab('chat');
  else if (location.hash === '#nodi' && isPhone()) setTab('list');
  chatPollSoon();
  connectEvents();
  // controllo periodico di riserva: ogni minuto senza tempo reale, ogni 5 minuti con
  setInterval(() => { if (!live || Date.now() - lastRefresh > 300000) refresh(); }, 60000);
  if (me.role === 'admin') updateBadge();
  // sul telefono, quando si torna alla pagina (app in primo piano), aggiorna subito
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    refresh(); chatPollSoon();
    if (!events || events.readyState === EventSource.CLOSED) connectEvents();
  });
}

// ---------- tempo reale: il Raspberry avvisa quando cambia qualcosa (Server-Sent Events)
let events = null, live = false, stateTimer = null;
function setLive(on) {
  live = on;
  $('radio').title = on ? 'Aggiornamento in tempo reale attivo' : 'Aggiornamento ogni minuto (tempo reale non disponibile)';
  $('radio').classList.toggle('live', on);
}
function connectEvents() {
  if (!window.EventSource) return;
  if (events) events.close();
  events = new EventSource('/api/events');
  events.addEventListener('hello', () => { if (!live && lastRefresh && Date.now() - lastRefresh > 5000) refresh(); setLive(true); });
  // più notizie ravvicinate (es. il giro dei ripetitori) = un solo aggiornamento
  events.addEventListener('state', () => { clearTimeout(stateTimer); stateTimer = setTimeout(refresh, 800); });
  events.addEventListener('chat', () => chatPollSoon());
  events.onerror = () => setLive(false);   // il browser riprova da solo (es. dopo un riavvio)
}
init();
