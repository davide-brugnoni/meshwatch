// MeshWatch — chat dalla radio gateway
// ---------- chat
// I messaggi arrivano alla radio anche a pagina chiusa e restano nel database del Raspberry.
// Il Raspberry avvisa la pagina appena c'è una novità (tempo reale); senza, la pagina
// chiede le novità ogni 4 secondi quando la chat è aperta, ogni 30 altrimenti.
const chat = { list: null, cur: null, lastId: 0, msgs: new Map(), timer: null };
const enc = new TextEncoder();
const STATUS = {
  queued: ['in coda (la radio sta finendo un\'altra richiesta)', false], sending: ['invio…', false],
  sent: ['inviato, in attesa di conferma', false], delivered: ['✓✓ consegnato', false],
  failed: ['✗ nessuna conferma di consegna', true],
};
const convKey = (kind, peer) => kind + ':' + peer;
const isChatOpen = () => document.body.classList.contains('tab-chat');
const canChat = () => me && ['admin', 'viewer'].includes(me.role);
function fmtTime(ts) { return new Date(ts * 1000).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' }); }
function fmtDay(ts) { return new Date(ts * 1000).toLocaleDateString('it-IT', { weekday: 'short', day: '2-digit', month: 'long' }); }

function setUnread(n) {
  [$('tab-chat'), document.querySelector('#mnav [data-t=chat]')].forEach(b => {
    if (!b) return;
    b.querySelectorAll('.count').forEach(x => x.remove());
    if (n > 0) b.append(el('span', { className: 'count', textContent: n > 99 ? '99+' : String(n) }));
  });
  document.title = n > 0 ? `(${n}) MeshWatch` : 'MeshWatch';
}

async function loadChat() {
  await loadChatList();
  if (chat.cur) openConv(chat.cur.kind, chat.cur.peer, chat.cur.name, true);
}

function renderConvs() {
  const L_ = chat.list; if (!L_) return;
  const sel = $('chat-target'), keep = sel.value;
  const chans = L_.targets.filter(t => t.kind === 'chan'), people = L_.targets.filter(t => t.kind === 'dm');
  const og = (label, arr) => { const g = el('optgroup', { label }); arr.forEach(t => g.append(el('option', { value: convKey(t.kind, t.peer), textContent: t.name + (t.type === 'room server' ? ' (room server)' : '') }))); return g; };
  sel.replaceChildren(el('option', { value: '', textContent: L_.targets.length ? 'Nuova conversazione…' : 'Nessun contatto o canale' }),
    ...(chans.length ? [og('Canali', chans)] : []), ...(people.length ? [og('Contatti', people)] : []));
  sel.value = keep;
  const box = $('chat-convs'); box.replaceChildren();
  if (!L_.conversations.length) box.append(el('p', { className: 'muted', style: 'padding:14px;margin:0', textContent: 'Ancora nessun messaggio.' }));
  L_.conversations.forEach(c => {
    const last = (c.dir === 'out' ? 'Tu: ' : c.sender ? c.sender + ': ' : '') + c.text;
    const item = el('div', { className: 'conv' + (chat.cur && convKey(chat.cur.kind, chat.cur.peer) === convKey(c.kind, c.peer) ? ' on' : '') },
      el('div', { className: 'conv-top' }, el('span', { className: 'conv-name' }, (c.kind === 'chan' ? '# ' : '') + (c.peer_name || c.peer),
        c.unread ? el('span', { className: 'count', textContent: String(c.unread) }) : null), el('span', { className: 'when', textContent: fmtAgo(c.ts) })),
      el('span', { className: 'conv-last', textContent: last }));
    item.onclick = () => openConv(c.kind, c.peer, c.peer_name || c.peer);
    box.append(item);
  });
}

function limitFor(kind) { return chat.list ? chat.list.limits[kind] : 150; }
function updateLen() {
  if (!chat.cur) return;
  const n = enc.encode($('chat-text').value).length, max = limitFor(chat.cur.kind);
  $('chat-len').textContent = `${n}/${max}`;
  $('chat-len').className = n > max ? 'bad' : '';
  $('chat-send').disabled = !n || n > max;
  const t = $('chat-text'); t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 120) + 'px';
}

async function openConv(kind, peer, name, keepScroll) {
  chat.cur = { kind, peer, name };
  document.body.classList.add('chat-open');
  $('chat-title').textContent = (kind === 'chan' ? '# ' : '') + name;
  $('chat-sub').textContent = kind === 'chan' ? 'canale: lo leggono tutti quelli che lo hanno' : 'messaggio privato';
  $('chat-form').hidden = false; $('chat-del').hidden = !(me && me.role === 'admin');
  let rows; try { rows = await api(`/api/chat/messages?kind=${kind}&peer=${encodeURIComponent(peer)}`); } catch (e) { return; }
  if (!chat.cur || chat.cur.peer !== peer || chat.cur.kind !== kind) return;
  chat.msgs = new Map(rows.map(m => [m.id, m]));
  renderMsgs(!keepScroll);
  updateLen();
  if (rows.some(m => m.dir === 'in' && !m.read)) { await post('/api/chat/read', { kind, peer }).catch(() => { }); loadChatList(); }
  renderConvs();
  if (!isPhone()) $('chat-text').focus();
}

function msgEl(m) {
  const st = m.dir === 'out' ? STATUS[m.status] || [m.status, false] : null;
  const meta = [fmtTime(m.ts)];
  if (m.dir === 'in') {
    if (m.snr != null) meta.push(`SNR ${(+m.snr).toFixed(1)}`);
    if (m.path_len != null && m.path_len !== 255) meta.push(hopsText(m.path_len));
  } else {
    if (chat.cur.kind === 'chan' && m.status === 'sent') meta.push('✓ trasmesso');
    else meta.push(st[0] + (m.attempts > 1 && m.status !== 'failed' ? ` (tentativo ${m.attempts})` : ''));
    if (me && me.username !== m.user && m.user) meta.push(m.user);
  }
  const metaEl = el('span', { className: 'meta' + (st && st[1] ? ' bad' : ''), textContent: meta.join(' · ') });
  if (m.dir === 'out' && m.status === 'failed') {
    const again = el('button', { textContent: 'Riprova' });
    again.onclick = () => sendChat(m.text, again);
    metaEl.append(again);
  }
  return el('div', { className: 'msg ' + m.dir }, m.dir === 'in' && m.sender ? el('span', { className: 'from', textContent: m.sender }) : null, m.text, metaEl);
}

function renderMsgs(scroll) {
  const box = $('chat-msgs');
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  box.replaceChildren();
  const list = [...chat.msgs.values()].sort((a, b) => a.id - b.id);
  if (!list.length) box.append(el('p', { id: 'chat-empty', textContent: chat.cur.kind === 'chan'
    ? 'Nessun messaggio su questo canale. Quello che scrivi lo ricevono tutti i nodi che hanno il canale.'
    : `Nessun messaggio con ${chat.cur.name}. Chi riceve vede come mittente il nome del gateway.` }));
  let day = null;
  list.forEach(m => {
    const d = fmtDay(m.ts);
    if (d !== day) { box.append(el('div', { className: 'day', textContent: d })); day = d; }
    box.append(msgEl(m));
  });
  if (scroll || atBottom) box.scrollTop = box.scrollHeight;
}

async function loadChatList() {
  try { chat.list = await api('/api/chat'); } catch (e) { return; }
  chat.lastId = Math.max(chat.lastId, chat.list.last_id || 0);
  setUnread(chat.list.unread); renderConvs();
}

async function chatPoll() {
  clearTimeout(chat.timer);
  if (!canChat()) return;
  try {
    if (!chat.list) await loadChatList();   // parte dall'ultimo messaggio, non da tutto lo storico
    const u = await api('/api/chat/updates?after=' + chat.lastId);
    let changed = false, newIn = false;
    const elsewhere = [];   // arrivati in una conversazione che non si sta guardando
    u.messages.forEach(m => {
      chat.lastId = Math.max(chat.lastId, m.id);
      const here = chat.cur && m.kind === chat.cur.kind && m.peer === chat.cur.peer;
      if (here) { chat.msgs.set(m.id, m); changed = true; if (m.dir === 'in') newIn = true; }
      if (m.dir === 'in' && !(here && isChatOpen() && !document.hidden)) elsewhere.push(m);
    });
    if (elsewhere.length) notifyMsgs(elsewhere);
    u.status.forEach(x => { const m = chat.msgs.get(x.id); if (m && (m.status !== x.status || m.attempts !== x.attempts)) { Object.assign(m, x); changed = true; } });
    if (changed && isChatOpen()) renderMsgs(false);
    if (newIn && isChatOpen() && !document.hidden) await post('/api/chat/read', { kind: chat.cur.kind, peer: chat.cur.peer }).catch(() => { });
    if (u.messages.length) await loadChatList();   // elenco a sinistra e non letti sempre aggiornati
    else setUnread(u.unread);
  } catch (e) { }
  // con il tempo reale i messaggi arrivano subito: il controllo periodico è solo di riserva
  const open_ = isChatOpen() && !document.hidden;
  chat.timer = setTimeout(chatPoll, live ? (open_ ? 30000 : 120000) : (open_ ? 4000 : 30000));
}
// avviso in alto a destra per i messaggi arrivati: si tocca per aprire la conversazione
function notifyMsgs(list) {
  const m = list[list.length - 1], n = $('msg-note');
  const name = m.kind === 'chan' ? '# ' + (m.peer_name || m.peer) : (m.peer_name || m.peer);
  const others = list.length - 1;
  const x = el('button', { className: 'x', textContent: '×', title: 'Chiudi', ariaLabel: 'Chiudi' });
  x.onclick = e => { e.stopPropagation(); n.hidden = true; };
  n.replaceChildren(x, el('b', { textContent: 'Nuovo messaggio · ' + name }),
    el('span', { textContent: (m.sender ? m.sender + ': ' : '') + m.text }),
    el('small', { textContent: (others ? `e altri ${others} · ` : '') + 'tocca per aprire' }));
  n.onclick = () => { n.hidden = true; setTab('chat'); openConv(m.kind, m.peer, m.peer_name || m.peer); };
  n.hidden = false;
  clearTimeout(n._h); n._h = setTimeout(() => n.hidden = true, 15000);
}
function chatPollSoon() { if (canChat()) { clearTimeout(chat.timer); chat.timer = setTimeout(chatPoll, 300); } }

async function sendChat(text, btn) {
  if (!chat.cur) return;
  text = text.trim(); if (!text) return;
  if (btn) btn.disabled = true;
  try {
    const r = await post('/api/chat', { kind: chat.cur.kind, peer: chat.cur.peer, text });
    chat.msgs.set(r.message.id, r.message);
    chat.lastId = Math.max(chat.lastId, r.message.id);
    renderMsgs(true);
    if (btn === $('chat-send')) { $('chat-text').value = ''; updateLen(); }
    loadChatList(); chatPollSoon();
  } catch (e) { if (e.message !== 'login') toast('Errore: ' + e.message, true); }
  finally { if (btn) btn.disabled = false; if (btn === $('chat-send')) updateLen(); }
}
$('chat-form').onsubmit = e => { e.preventDefault(); sendChat($('chat-text').value, $('chat-send')); };
$('chat-text').oninput = updateLen;
$('chat-text').onkeydown = e => {   // Invio manda, Maiusc+Invio non serve: i messaggi MeshCore sono su una riga
  if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); if (!$('chat-send').disabled) $('chat-form').requestSubmit(); }
};
$('chat-open').onclick = () => {
  const v = $('chat-target').value; if (!v) return;
  const t = chat.list.targets.find(x => convKey(x.kind, x.peer) === v);
  if (t) openConv(t.kind, t.peer, t.name);
  $('chat-target').value = '';
};
$('chat-back').onclick = () => { document.body.classList.remove('chat-open'); chat.cur = null; renderConvs(); };
$('chat-del').onclick = e => {
  if (!chat.cur || !confirm(`Cancellare tutti i messaggi con ${chat.cur.name} dal Raspberry?`)) return;
  action(e.target, () => del(`/api/chat?kind=${chat.cur.kind}&peer=${encodeURIComponent(chat.cur.peer)}`), 'Conversazione cancellata')
    .then(() => { chat.msgs.clear(); renderMsgs(true); loadChatList(); });
};
function writeTo(c) {   // dalla rubrica: apre la chat con quel contatto
  setTab('chat');
  const go = () => openConv('dm', c.public_key.slice(0, 12), c.name);
  chat.list ? go() : setTimeout(go, 400);
}
