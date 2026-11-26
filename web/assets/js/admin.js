// MeshWatch — gestione: dispositivi, ripetitori, utenti, gateway, sistema, aggiornamenti, backup, rubrica, Telegram
// ---------- sezioni della gestione (una alla volta; l'ultima scelta resta nel browser)
function setAdmSec(s) {
  if (!document.querySelector(`.adm-sec[data-sec="${s}"]`)) s = 'dispositivi';
  document.querySelectorAll('.adm-sec').forEach(x => x.classList.toggle('on', x.dataset.sec === s));
  document.querySelectorAll('#adm-nav button').forEach(b => {
    b.classList.toggle('on', b.dataset.sec === s);
    if (b.dataset.sec === s && b.scrollIntoView) b.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  });
  try { localStorage.setItem('admSec', s); } catch (e) { }
  $('admin-scroll').scrollTop = 0;
}
document.querySelectorAll('#adm-nav button').forEach(b => b.onclick = () => setAdmSec(b.dataset.sec));
try { setAdmSec(localStorage.getItem('admSec') || 'dispositivi'); } catch (e) { setAdmSec('dispositivi'); }

// ---------- gestione
const FREE = '__altro__';
function contactSelect(sel, types, exclude, current) {
  const opts = adm.contacts.filter(c => types.includes(c.type) && (!exclude.includes(c.name) || c.name === current));
  sel.replaceChildren(
    ...opts.map(c => el('option', { value: c.name, textContent: `${c.name} (${c.type})` })),
    el('option', { value: FREE, textContent: opts.length ? 'Altro, scrivo il nome…' : 'Nessun contatto visto: scrivo il nome…' }));
  const free = sel.closest('form').querySelector('.free');
  const upd = () => { free.hidden = sel.value !== FREE; };
  sel.onchange = upd;
  if (current && !opts.some(c => c.name === current)) { sel.value = FREE; free.querySelector('input').value = current; }
  else if (current) sel.value = current;
  upd();
}
const pickedName = (f, sel, free) => f[sel].value === FREE ? f[free].value.trim() : f[sel].value;
function openForm(form, onSubmit) {
  // chiude gli altri moduli a comparsa (quelli con "Annulla"), non gateway e manutenzione
  document.querySelectorAll('form.edit').forEach(x => { if (x.querySelector('.cancel')) x.hidden = true; });
  const sec = form.closest('.adm-sec'); if (sec && !sec.classList.contains('on')) setAdmSec(sec.dataset.sec);
  form.hidden = false; form.querySelector('.cancel').onclick = () => form.hidden = true;
  form.onsubmit = async e => { e.preventDefault(); const r = await action(form.querySelector('.primary'), onSubmit); if (r) form.hidden = true; };
  form.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function loadAdmin() {
  try { adm = await api('/api/admin'); } catch (e) { return; }
  // dispositivi
  const tl = $('adm-trackers'); tl.replaceChildren();
  if (!adm.trackers.length) tl.append(el('p', { className: 'muted', textContent: 'Nessun dispositivo.' }));
  adm.trackers.forEach(t => {
    const edit = el('button', { className: 'small', textContent: 'Modifica' }); edit.onclick = () => trackerForm(t);
    const rules = el('button', { className: 'small', textContent: 'Soglie' }); rules.onclick = () => rulesForm(t);
    const rm = el('button', { className: 'small danger', textContent: 'Elimina' });
    rm.onclick = () => { if (confirm(`Eliminare ${t.label}? Verrà cancellato anche lo storico delle posizioni.`)) action(rm, () => del(`/api/trackers/${t.id}`), 'Dispositivo eliminato'); };
    tl.append(el('div', { className: 'item' },
      el('span', { className: 'name' }, swatch(t.color), t.label),
      el('span', { className: 'info', textContent: `contatto ${t.contact} · ogni ${t.interval_min} min${t.moving_interval_min ? ` (${t.moving_interval_min} in movimento)` : ''} · ${t.active_hours || 'sempre'} · avviso dopo ${t.offline_alert_min} min · batteria < ${t.low_battery_v} V${t.paused ? ' · IN PAUSA' : ''}` }),
      el('span', { className: 'acts' }, edit, rules, rm)));
  });
  // ripetitori
  const rl = $('adm-reps'); rl.replaceChildren();
  if (!adm.repeaters.length) rl.append(el('p', { className: 'muted', textContent: 'Nessun ripetitore.' }));
  adm.repeaters.forEach(r => {
    const edit = el('button', { className: 'small', textContent: 'Cambia password' }); edit.onclick = () => repForm(r);
    const rm = el('button', { className: 'small danger', textContent: 'Rimuovi' });
    rm.onclick = () => { if (confirm(`Rimuovere ${r.name} dal monitoraggio?`)) action(rm, () => del('/api/repeaters/' + encodeURIComponent(r.name)), 'Ripetitore rimosso'); };
    rl.append(el('div', { className: 'item' }, el('span', { className: 'name' }, r.name),
      el('span', { className: 'info', textContent: r.has_password ? 'password impostata' : 'senza password' }), el('span', { className: 'acts' }, edit, rm)));
  });
  // utenti
  const ul = $('adm-users'); ul.replaceChildren();
  adm.users.forEach(u => {
    const pw = el('button', { className: 'small', textContent: 'Cambia password' }); pw.onclick = () => userForm(u);
    const role = el('button', { className: 'small', textContent: u.role === 'admin' ? 'Rendi viewer' : 'Rendi admin' });
    role.onclick = () => action(role, () => put('/api/users/' + encodeURIComponent(u.username), { role: u.role === 'admin' ? 'viewer' : 'admin' }), 'Ruolo cambiato');
    const rm = el('button', { className: 'small danger', textContent: 'Elimina' });
    rm.onclick = () => { if (confirm(`Eliminare l'utente ${u.username}?`)) action(rm, () => del('/api/users/' + encodeURIComponent(u.username)), 'Utente eliminato').then(() => { if (u.username === adm.me) location.reload(); }); };
    ul.append(el('div', { className: 'item' }, el('span', { className: 'name', textContent: u.username + (u.username === adm.me ? ' (tu)' : '') }),
      el('span', { className: 'info', textContent: u.role }), el('span', { className: 'acts' }, pw, role, rm)));
  });
  const note = $('users-note');
  note.className = 'note';
  note.textContent = 'Admin gestisce tutto. Viewer vede mappa e stato e può chiedere la posizione, ma non gestisce nulla. '
    + (me.public_map ? 'La mappa è visibile anche senza login (sola lettura).' : 'Senza login la mappa non è visibile.');
  loadAudit();
}

async function loadAudit() {
  let rows; try { rows = await api('/api/audit'); } catch (e) { return; }
  const box = $('adm-audit'); box.replaceChildren();
  if (!rows.length) box.append(el('p', { className: 'muted', textContent: 'Nessuna attività registrata.' }));
  rows.forEach(r => {
    const d = new Date(r.ts * 1000);
    const when = d.toLocaleDateString('it-IT', { day: '2-digit', month: '2-digit' }) + ' ' + d.toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' });
    const bad = /fallito|bloccato/.test(r.action);
    box.append(el('div', {},
      el('span', { className: 'who', textContent: when }),
      el('span', { className: 'who', title: r.ip ? `${r.user} da ${r.ip}` : r.user, textContent: r.user || '—' }),
      el('span', { className: 'what' + (bad ? ' bad' : ''), textContent: r.action + (r.detail ? ' · ' + r.detail : '') + (r.ip ? ` (${r.ip})` : '') })));
  });
}


function trackerForm(t) {
  const f = $('form-tracker');
  contactSelect(f.contact_sel, ['companion', 'sensore', 'altro'], adm.trackers.map(x => x.contact), t && t.contact);
  f.label.value = t ? t.label : '';
  f.interval_min.value = t ? t.interval_min : 30;
  f.moving_interval_min.value = t ? (t.moving_interval_min || 0) : 0;
  const h = t ? t.active_hours : '05:00-21:00';
  f.always.checked = !h; if (h) [f.h_from.value, f.h_to.value] = h.split('-');
  f.offline_alert_min.value = t ? t.offline_alert_min : 90;
  f.low_battery_v.value = t ? t.low_battery_v : 3.5;
  openForm(f, () => {
    const contact = pickedName(f, 'contact_sel', 'contact_free');
    if (!contact) throw new Error('scegli o scrivi il nome del contatto');
    const mv = +f.moving_interval_min.value;
    if (mv && (mv < 5 || mv > +f.interval_min.value)) throw new Error('l\'intervallo in movimento va da 5 minuti a quello normale');
    const body = { contact, label: f.label.value.trim() || contact, interval_min: +f.interval_min.value, moving_interval_min: mv,
      active_hours: f.always.checked ? '' : `${f.h_from.value}-${f.h_to.value}`,
      offline_alert_min: +f.offline_alert_min.value, low_battery_v: +f.low_battery_v.value };
    return t ? put(`/api/trackers/${t.id}`, body) : post('/api/trackers', body);
  });
}
// soglie dei sensori di un dispositivo: avvisi su Telegram
async function rulesForm(t) {
  const f = $('form-rules');
  let d; try { d = await api(`/api/trackers/${t.id}/rules`); } catch (e) { if (e.message !== 'login') toast('Errore: ' + e.message, true); return; }
  $('rules-title').textContent = `Soglie dei sensori · ${t.label}`;
  const key = s => `${s.type}:${s.channel}`;
  const fmt = (r, v) => (+v).toFixed(r.dec) + (r.unit ? ' ' + r.unit : '');
  f.sensor.replaceChildren(...(d.sensors.length ? d.sensors.map(s => el('option', { value: key(s), textContent: `${s.label} (canale ${s.channel})` }))
    : [el('option', { value: '', textContent: 'Nessun sensore ricevuto finora' })]));
  f.sensor.disabled = f.value.disabled = !d.sensors.length;
  $('rules-list').replaceChildren(...(d.rules.length ? d.rules.map(r => {
    const rm = el('button', { className: 'small danger', textContent: 'Rimuovi' });
    rm.onclick = () => action(rm, () => del(`/api/trackers/${t.id}/rules/${r.id}`), 'Soglia rimossa').then(() => rulesForm(t));
    return el('div', { className: 'item' }, el('span', { className: 'info', textContent: `${r.label} (canale ${r.channel}) ${r.op === '<' ? 'sotto' : 'sopra'} ${fmt(r, r.value)}` }),
      el('span', { className: 'acts' }, rm));
  }) : [el('p', { className: 'muted', style: 'margin:0', textContent: 'Nessuna soglia impostata.' })]));
  openForm(f, async () => {
    const s = d.sensors.find(x => key(x) === f.sensor.value);
    if (!s) throw new Error('nessun sensore da controllare');
    await post(`/api/trackers/${t.id}/rules`, { type: s.type, channel: s.channel, op: f.op.value, value: +f.value.value });
    f.value.value = ''; toast('Soglia aggiunta');
    setTimeout(() => rulesForm(t), 0);
    return null;   // il modulo resta aperto, con la nuova soglia in elenco
  });
}
function repForm(r) {
  const f = $('form-rep');
  contactSelect(f.name_sel, ['ripetitore', 'room server'], adm.repeaters.map(x => x.name), r && r.name);
  f.name_sel.disabled = !!r; f.name_free.disabled = !!r; f.password.value = '';
  f.password.placeholder = r ? 'nuova password' : '';
  openForm(f, () => {
    const name = r ? r.name : pickedName(f, 'name_sel', 'name_free');
    if (!name) throw new Error('scegli o scrivi il nome del ripetitore');
    return post('/api/repeaters', { name, password: f.password.value });
  });
}
function userForm(u) {
  const f = $('form-user');
  f.username.value = u ? u.username : ''; f.username.disabled = !!u; f.password.value = '';
  f.role.value = u ? u.role : (adm.users.length ? 'viewer' : 'admin'); f.role.disabled = !!u;
  openForm(f, async () => {
    if (f.password.value.length < 6) throw new Error('password di almeno 6 caratteri');
    if (u) return put('/api/users/' + encodeURIComponent(u.username), { password: f.password.value });
    const first = !adm.users.length;
    const r = await post('/api/users', { username: f.username.value.trim(), password: f.password.value, role: f.role.value });
    if (first) { toast('Primo utente creato: ora accedi'); setTimeout(() => location.reload(), 1200); }
    return r;
  });
}

// ---------- gateway
let gw = null;
async function loadGateway() {
  const box = $('gw-info'), f = $('form-gw');
  try { gw = await api('/api/gateway'); }
  catch (e) { if (e.message !== 'login') box.replaceChildren(el('p', { className: 'muted', textContent: 'Gateway non disponibile: ' + e.message })); f.hidden = true; $('gw-chart').replaceChildren(); return; }
  f.hidden = false;
  const v = (x, suf = '') => x == null || x === '' ? '—' : x + suf;
  box.replaceChildren(
    cell('Modello', v(gw.model)), cell('Firmware', v(gw.firmware) + (gw.fw_build ? ' (' + gw.fw_build + ')' : '')),
    cell('Chiave', gw.public_key ? gw.public_key.slice(0, 12) + '…' : '—'),
    cell('Alimentazione', gw.battery_mv ? (gw.battery_mv / 1000).toFixed(2) + ' V' : '—'),
    cell('Uptime', fmtUp(gw.uptime)), cell('Coda TX', v(gw.queue_len)),
    cell('Rumore', v(gw.noise_floor, ' dBm')), cell('Ultimo RSSI', v(gw.last_rssi, ' dBm')), cell('Ultimo SNR', v(gw.last_snr)),
    ...packetCells({ nb_recv: gw.recv, nb_sent: gw.sent, recv_flood: gw.flood_rx, recv_direct: gw.direct_rx,
      sent_flood: gw.flood_tx, sent_direct: gw.direct_tx, tx_air: gw.tx_air_secs, rx_air: gw.rx_air_secs,
      recv_errors: gw.recv_errors }),
    ...(gw.errors ? [cell('Errori', v(gw.errors))] : []),
    cell('Contatti', v(gw.contacts)));
  drawGatewayChart();
  f.gw_name.value = gw.name || ''; f.tx_power.value = gw.tx_power ?? ''; f.tx_power.max = gw.max_tx_power || 30;
  f.lat.value = gw.lat ?? ''; f.lon.value = gw.lon ?? ''; f.share_location.checked = !!gw.share_location;
  f.radio_freq.value = gw.radio_freq ?? ''; f.radio_bw.value = String(gw.radio_bw ?? '');
  f.radio_sf.value = String(gw.radio_sf ?? ''); f.radio_cr.value = String(gw.radio_cr ?? '');
}
// ---------- firmware MeshCore
let fw = null;
async function loadFirmware(path = '/api/firmware', opts) {
  const box = $('fw-info');
  try { fw = await api(path, opts); }
  catch (e) {
    if (opts) throw e;   // azioni dai pulsanti: l'errore lo mostra action()
    if (e.message !== 'login') box.replaceChildren(el('p', { className: 'muted', textContent: 'Firmware: ' + e.message }));
    return;
  }
  const busy = ['check', 'download', 'flash', 'reconnect'].includes(fw.job.state);
  box.replaceChildren(
    cell('Modello', fw.model || '—'), cell('Installato', fw.installed || '—'),
    cell('Ultimo rilascio', fw.latest ? fw.latest.version : '—'),
    cell('Stato', busy ? '⏳ in corso' : fw.available ? '⬆️ da aggiornare' : fw.installed && fw.latest ? '✅ aggiornato' : '—'));
  const msg = $('fw-msg');
  const recent = ['done', 'error'].includes(fw.job.state) && Date.now() / 1000 - fw.job.ts < 3600;
  const text = busy ? '⏳ ' + fw.job.message : recent ? (fw.job.state === 'done' ? '✅ ' : '❌ ') + fw.job.message
    : fw.problems.length ? 'Non si può aggiornare: ' + fw.problems.join('; ')
    : fw.tool_missing ? 'Al primo aggiornamento viene installato lo strumento per programmare la scheda (serve Internet).' : '';
  msg.hidden = !text; msg.textContent = text; msg.className = 'note' + ((fw.job.state === 'error' && recent) || (!busy && fw.problems.length) ? ' warn' : '');
  const sel = $('form-fw').board, opts2 = [...new Set([...(fw.suggested || []), ...(fw.boards || [])])];
  if (fw.board && !opts2.includes(fw.board)) opts2.unshift(fw.board);
  sel.replaceChildren(el('option', { value: '', textContent: opts2.length ? '— scegli —' : 'nessuna scheda' }),
    ...opts2.map(b => el('option', { value: b, textContent: b + ((fw.suggested || []).includes(b) ? ' ★' : '') })));
  sel.value = fw.board || '';
  sel.disabled = busy;
  $('fw-go').disabled = busy || !fw.latest || fw.problems.length > 0;
  $('fw-go').textContent = fw.available || !fw.installed ? 'Aggiorna firmware' : 'Reinstalla firmware';
  const latestRep = fw.latest_repeater;
  const ver = v => (v || '').match(/v?(\d+)\.(\d+)(?:\.(\d+))?/);
  const older = (a, b) => { const x = ver(a), y = ver(b); if (!x || !y) return false; for (let i = 1; i < 4; i++) { const d = (+x[i] || 0) - (+y[i] || 0); if (d) return d < 0; } return false; };
  $('fw-reps').replaceChildren(...fw.repeaters.map(r => el('div', { className: 'item' },
    el('span', { className: 'name' }, dot(!r.version ? '' : older(r.version, latestRep) ? 'ko' : 'ok'), r.name),
    el('span', { className: 'info', textContent: (r.version || 'versione non ancora letta') +
      (older(r.version, latestRep) ? ` · disponibile ${latestRep}: sul posto /terminale → start ota` : '') +
      (r.ts ? ` · letta ${fmtAgo(r.ts)}` : '') }))));
  $('fw-reps-read').hidden = !fw.repeaters.length;
}
$('form-fw').board.onchange = e => action(null, () => loadFirmware('/api/firmware/board', { method: 'PUT', body: JSON.stringify({ board: e.target.value || null }) }), 'Scheda salvata');
$('fw-check').onclick = e => action(e.target, () => loadFirmware('/api/firmware/check', { method: 'POST' }));
$('fw-reps-read').onclick = e => action(e.target, () => loadFirmware('/api/firmware/repeaters', { method: 'POST' }), 'Versioni lette');
$('form-fw').onsubmit = async e => {
  e.preventDefault();
  if (!fw || !fw.latest) return;
  const board = e.target.board.value;
  if (!board) { toast('Scegli la scheda del gateway', true); return; }
  if (!confirm(`Scrivere il firmware ${fw.latest.version} per ${board} nella radio del gateway?\n` +
    'Per qualche minuto niente radio (posizioni, ripetitori, chat). Non scollegare il cavo USB.\n' +
    'Se la scheda scelta non è quella giusta la radio può non avviarsi.')) return;
  await action($('fw-go'), () => post('/api/firmware/update', { board, confirm: true }), 'Aggiornamento del firmware avviato');
  loadFirmware();
};

// controllo automatico del firmware: giorno e ora per gateway e ripetitori
async function loadFirmwareSchedule(r) {
  if (!r) { try { r = await api('/api/firmware/schedule'); } catch (e) { return; } }
  document.querySelectorAll('form.fw-sched').forEach(f => {
    const s = r[f.dataset.kind];
    if (f.day.options.length !== r.days.length)
      f.day.replaceChildren(...r.days.map((d, i) => el('option', { value: i, textContent: d })));
    f.enabled.checked = s.enabled; f.day.value = s.day; f.time.value = s.time;
    f.day.disabled = f.time.disabled = !s.enabled;
    const nxt = s.next ? new Date(s.next.replace(' ', 'T')) : null;
    f.querySelector('.note').textContent = !s.enabled ? 'Spento: nessun avviso automatico (resta "Controlla ora").'
      : `Attivo: ${s.text}. Prossimo: ${nxt.toLocaleDateString('it-IT', { weekday: 'long', day: 'numeric', month: 'long' })} alle ${s.time}.`;
  });
}
document.querySelectorAll('form.fw-sched').forEach(f => {
  f.enabled.onchange = () => { f.day.disabled = f.time.disabled = !f.enabled.checked; };
  f.onsubmit = e => {
    e.preventDefault();
    action(f.querySelector('.primary'), () => put('/api/firmware/schedule',
      { kind: f.dataset.kind, enabled: f.enabled.checked, day: +f.day.value, time: f.time.value }),
      r => (f.dataset.kind === 'gateway' ? 'Gateway: ' : 'Ripetitori: ') + r[f.dataset.kind].text).then(r => loadFirmwareSchedule(r || undefined));
  };
});

// storico del gateway (salvato a ogni giro dei ripetitori): stessi grafici dei ripetitori
function drawGatewayChart() {
  const box = $('gw-chart'), h = (gw && gw.history) || [];
  const tabs = metricTabs(h, 'gw', drawGatewayChart);
  box.replaceChildren(...(tabs ? [tabs, metricChart(h, 'gw')]
    : [el('p', { className: 'muted', style: 'margin:6px 0 0;font-size:12.5px', textContent: 'Grafici: lo storico si riempie a ogni giro dei ripetitori' })]));
}
$('form-gw').onsubmit = async e => {
  e.preventDefault();
  const f = e.target;
  const body = { name: f.gw_name.value.trim(), tx_power: +f.tx_power.value, share_location: f.share_location.checked,
    radio_freq: +f.radio_freq.value, radio_bw: +f.radio_bw.value, radio_sf: +f.radio_sf.value, radio_cr: +f.radio_cr.value };
  if (f.lat.value !== '' && f.lon.value !== '') { body.lat = +f.lat.value; body.lon = +f.lon.value; }
  const radioChanged = gw && (body.radio_freq !== gw.radio_freq || body.radio_bw !== gw.radio_bw || body.radio_sf !== gw.radio_sf || body.radio_cr !== gw.radio_cr);
  if (radioChanged) {
    if (!confirm(`Cambiare i parametri radio del gateway in ${body.radio_freq} MHz, BW ${body.radio_bw}, SF${body.radio_sf}, CR${body.radio_cr}?\nSe non coincidono con la rete, il gateway non sentirà più i nodi.`)) return;
    body.confirm_radio = true;
  }
  const r = await action(f.querySelector('.primary'), () => post('/api/gateway', body),
    r => r.radio_changed ? 'Salvato. Se la radio non cambia, riavvia il gateway' : 'Gateway aggiornato');
  if (r) loadGateway();
};
$('gw-reload').onclick = () => loadGateway();
$('gw-advert').onclick = e => action(e.target, () => post('/api/advert'), 'Advert inviato');
$('gw-reboot').onclick = e => { if (confirm('Riavviare la radio gateway? Torna disponibile in circa 30 secondi.')) action(e.target, () => post('/api/gateway/reboot'), 'Gateway in riavvio').then(() => setTimeout(loadGateway, 30000)); };


// ---------- sistema
async function loadSystem() {
  let si;
  try { si = await api('/api/system'); } catch (e) { return; }
  const v = (x, suf = '') => x == null ? '—' : x + suf;
  $('sys-info').replaceChildren(
    cell('Modello', v(si.model)), cell('Temperatura', v(si.temp_c, ' °C')), cell('Acceso da', fmtUp(si.uptime)),
    cell('Carico CPU', v(si.load)), cell('RAM libera', si.mem_total_mb ? `${si.mem_free_mb}/${si.mem_total_mb} MB` : '—'),
    cell('Disco libero', `${v(si.disk_free_gb)}/${v(si.disk_total_gb)} GB`),
    cell('Alimentazione', si.undervoltage_now ? 'TENSIONE BASSA' : si.undervoltage_since_boot ? 'bassa in passato' : si.undervoltage_now === false ? 'ok' : '—'));
  const w = $('sys-warn');
  w.hidden = !(si.undervoltage_now || si.undervoltage_since_boot);
  w.textContent = si.undervoltage_now ? 'L\'alimentatore non dà abbastanza corrente adesso: rischio di blocchi e di danni alla SD. Usa un alimentatore 5,1 V 2,5 A e un cavo corto.'
    : 'Dall\'ultimo avvio c\'è stata almeno una caduta di tensione: controlla alimentatore e cavo.';
  const ok = si.can_reboot && si.can_poweroff;
  $('sys-reboot').disabled = !si.can_reboot; $('sys-poweroff').disabled = !si.can_poweroff;
  $('sys-note').textContent = ok ? 'Dopo lo spegnimento, per riaccendere il Raspberry stacca e riattacca l\'alimentazione. La radio resta alimentata via USB finché c\'è corrente.'
    : 'Riavvio e spegnimento non sono ancora abilitati: serve un comando una tantum via SSH (vedi istruzioni).';
}
$('sys-reload').onclick = () => { loadSystem(); loadGateway(); };
$('sys-reboot').onclick = e => { if (confirm('Riavviare il Raspberry? La pagina torna disponibile in circa un minuto.')) action(e.target, () => post('/api/system/reboot'), 'Riavvio in corso…'); };
$('sys-poweroff').onclick = e => { if (confirm('Spegnere il Raspberry?\nPer riaccenderlo dovrai staccare e riattaccare l\'alimentazione.')) action(e.target, () => post('/api/system/poweroff'), 'Spegnimento in corso…'); };
$('sys-gw-reboot').onclick = e => { if (confirm('Riavviare la radio gateway? Torna disponibile in circa 30 secondi.')) action(e.target, () => post('/api/gateway/reboot'), 'Gateway in riavvio').then(() => setTimeout(loadGateway, 30000)); };

// ---------- aggiornamento programma
let updPoll = null;
const UPD_STEPS = { download: 'Download', check: 'Controllo', deps: 'Librerie', install: 'Installazione', restarting: 'Riavvio', error: 'Errore' };
function renderUpdate(u) {
  const v = (x, suf = '') => x == null || x === '' ? '—' : x + suf;
  const sha = x => x ? x.slice(0, 7) : null;
  const day = d => d ? new Date(d).toLocaleDateString('it-IT', { day: '2-digit', month: '2-digit', year: 'numeric' }) : '';
  const inst = u.installed;
  $('upd-info').replaceChildren(
    cell('Versione', v(u.version)),
    cell('Installata', inst.sha ? `${sha(inst.sha)} ${day(inst.date)}` : inst.source === 'sd' ? 'dal kit SD' : '—'),
    cell('Modalità', u.mode === 'git' ? 'git pull' : 'download GitHub'),
    cell('Ultima su GitHub', u.latest ? `${sha(u.latest.sha)} ${day(u.latest.date)}` : u.checked_at ? '—' : 'non controllata'));
  const warn = [];
  if (u.rolled_back) warn.push(`L'ultimo aggiornamento non partiva: il ${u.rolled_back} è stata ripristinata la versione precedente.`);
  if (u.problems.length) warn.push('Aggiornamento dalla pagina non disponibile: ' + u.problems.join('; ') + '. '
    + (u.mode === 'git' ? 'Rilancia install.sh oppure aggiorna con git pull.'
      : 'Per abilitarlo, una volta sola: sd-kit\\ABILITA-AGGIORNAMENTI.bat da Windows, oppure via SSH: curl -fsSL https://raw.githubusercontent.com/' + u.repo + '/main/tools/abilita-aggiornamenti.sh | sudo bash'));
  $('upd-warn').hidden = !warn.length; $('upd-warn').textContent = warn.join(' ');
  const job = u.job, busy = ['download', 'check', 'deps', 'install', 'restarting'].includes(job.state);
  const log = $('upd-log');
  log.hidden = !(busy || job.state === 'error' || u.latest);
  log.className = 'upd-log' + (job.state === 'error' ? ' note warn' : '');
  log.textContent = busy || job.state === 'error' ? `${UPD_STEPS[job.state]}: ${job.message}`
    : u.latest ? (u.available ? `Disponibile: ${u.latest.message}` : 'Hai già l\'ultima versione.') : '';
  $('upd-apply').disabled = busy || !u.available || u.problems.length > 0;
  $('upd-check').disabled = busy;
  clearTimeout(updPoll);
  updateBadge();
  if (job.state === 'restarting') waitRestart();
  else if (busy) updPoll = setTimeout(loadUpdate, 2000);
}
async function loadUpdate() {
  try { renderUpdate(await api('/api/update')); } catch (e) { if (e.message !== 'login') $('upd-info').replaceChildren(el('p', { className: 'muted', textContent: 'Non disponibile: ' + e.message })); }
}
async function waitRestart() {
  const log = $('upd-log'); log.hidden = false;
  log.textContent = 'Riavvio del servizio in corso… la pagina si ricarica da sola.';
  const t0 = Date.now();
  await new Promise(r => setTimeout(r, 5000));
  while (Date.now() - t0 < 30 * 60000) {
    try { const r = await fetch('/api/me', { cache: 'no-store' }); if (r.ok || r.status === 401) { location.reload(); return; } } catch (e) { }
    await new Promise(r => setTimeout(r, 3000));
  }
  log.textContent = 'Il servizio non risponde ancora: ricarica la pagina tra qualche minuto.';
}
$('upd-check').onclick = async e => {
  const b = e.target; b.disabled = true;
  try { renderUpdate(await post('/api/update/check')); } catch (err) { if (err.message !== 'login') toast('Errore: ' + err.message, true); }
  b.disabled = false;
};
$('upd-apply').onclick = async e => {
  if (!confirm('Aggiornare MeshWatch all\'ultima versione?\nIl servizio si riavvia: per circa un minuto la pagina e il bot non rispondono.')) return;
  const b = e.target; b.disabled = true;
  try { await post('/api/update/apply'); toast('Aggiornamento avviato'); } catch (err) { if (err.message !== 'login') toast('Errore: ' + err.message, true); }
  loadUpdate();
};

// pallino su "Gestione" quando c'è una nuova versione (controllo automatico ogni 12 ore)
async function updateBadge() {
  let u; try { u = await api('/api/update'); } catch (e) { return; }
  document.querySelectorAll('.badge').forEach(b => b.remove());
  if (!u.available || !u.latest) return;
  const title = 'Nuova versione disponibile: ' + u.latest.message;
  [$('tab-admin'), document.querySelector('#mnav [data-t=admin]'), document.querySelector('#adm-nav [data-sec=sistema]')]
    .forEach(b => b.append(el('span', { className: 'badge', title })));
}

// ---------- manutenzione e backup
const fmtSize = b => b == null ? '—' : b > 1073741824 ? (b / 1073741824).toFixed(1) + ' GB' : b > 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.round(b / 1024) + ' kB';
async function loadMaintenance() {
  let m; try { m = await api('/api/maintenance'); } catch (e) { return; }
  $('mnt-info').replaceChildren(
    cell('Database', fmtSize(m.db_size)), cell('Posizioni salvate', String(m.positions)),
    cell('Dati dal', m.oldest ? new Date(m.oldest * 1000).toLocaleDateString('it-IT') : '—'),
    cell('Ultima pulizia', m.last_purge ? fmtTs(m.last_purge) + (m.last_purge_deleted ? ` (${m.last_purge_deleted} righe)` : '') : 'mai'),
    cell('Ultimo backup Telegram', m.last_backup ? fmtTs(m.last_backup) : 'mai'));
  $('form-mnt').retention_days.value = m.retention_days;
  $('mnt-purge').disabled = !m.retention_days;
  const b = $('form-tgb');
  b.backup_telegram.checked = m.backup_telegram; b.backup_telegram.disabled = !m.telegram;
  b.querySelector('.primary').disabled = !m.telegram;
  $('mnt-telegram').disabled = !m.telegram;
  $('tgb-last').textContent = !m.telegram ? 'Telegram non configurato.'
    : 'Ultimo backup inviato: ' + (m.last_backup ? fmtTs(m.last_backup) : 'mai') + '.'
      + (m.backup_protected ? ' 🔒 Protetto da password.' : ' Senza password (si imposta in Sistema → Manutenzione e backup).');
  const st = $('bkpw-state');
  st.className = 'lock ' + (m.backup_protected ? 'on' : 'off');
  st.textContent = !m.backup_crypto ? 'Per proteggere i backup serve la libreria pyzipper: aggiorna MeshWatch.'
    : m.backup_protected ? '🔒 Backup protetti da password (AES-256)' : '🔓 Backup senza password';
  const p = $('form-bkpw');
  p.pw.value = p.pw2.value = '';
  [p.pw, p.pw2, p.querySelector('.primary')].forEach(x => x.disabled = !m.backup_crypto);
  p.querySelector('.primary').textContent = m.backup_protected ? 'Cambia password' : 'Proteggi i backup';
  $('bkpw-remove').hidden = !m.backup_protected;
}
$('form-bkpw').onsubmit = e => {
  e.preventDefault(); const f = e.target;
  action(f.querySelector('.primary'), () => {
    if (f.pw.value !== f.pw2.value) throw new Error('le due password non coincidono');
    return put('/api/maintenance', { backup_password: f.pw.value });
  }, 'Backup protetti da password: annotala in un posto sicuro').then(loadMaintenance);
};
$('bkpw-remove').onclick = e => {
  if (!confirm('Togliere la password? I prossimi backup saranno zip normali, leggibili da chiunque li abbia.')) return;
  action(e.target, () => put('/api/maintenance', { backup_password: '' }), 'Password del backup tolta').then(loadMaintenance);
};
$('form-mnt').onsubmit = e => {
  e.preventDefault(); const f = e.target;
  action(f.querySelector('.primary'), () => put('/api/maintenance', { retention_days: +f.retention_days.value }), 'Impostazioni salvate')
    .then(loadMaintenance);
};
$('form-tgb').onsubmit = e => {
  e.preventDefault(); const f = e.target;
  action(f.querySelector('.primary'), () => put('/api/maintenance', { backup_telegram: f.backup_telegram.checked }),
    f.backup_telegram.checked ? 'Backup settimanale attivo' : 'Backup settimanale spento').then(loadMaintenance);
};

// ---------- ripristino di un backup
let rst = null;
const fmtWhen = ts => ts ? new Date(ts * 1000).toLocaleString('it-IT', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
async function loadRestore() {
  let s; try { s = await api('/api/restore'); } catch (e) { return; }
  $('rst-undo-row').hidden = !s.prev;
  if (s.prev) $('rst-undo-note').textContent = `rimette lo stato del ${fmtWhen(s.prev.ts)} (${[s.prev.data && 'dati', s.prev.config && 'configurazione'].filter(Boolean).join(' e ')})`;
  const l = s.last, last = $('rst-last');
  last.hidden = !l;
  if (l) {
    last.className = 'note' + (l.ok ? '' : ' warn');
    last.textContent = (l.source === 'annullamento' ? 'Ultimo annullamento' : 'Ultimo ripristino') + `: ${fmtWhen(l.applied)}`
      + (l.backup_ts && l.source !== 'annullamento' ? `, backup del ${fmtWhen(l.backup_ts)}` : '')
      + (l.ok ? ' — riuscito.' : ' — problemi: ' + l.errors.join('; '));
  }
  rst = { ...(rst || {}), status: s };
}
function b64(s) { return btoa(String.fromCharCode(...new TextEncoder().encode(s))); }
$('form-rst').onsubmit = async e => {
  e.preventDefault();
  const f = e.target, file = f.file.files[0], btn = f.querySelector('.primary');
  if (!file) return;
  btn.disabled = true; btn.textContent = 'Controllo in corso…';
  try {
    const r = await fetch('/api/restore/check', { method: 'POST', credentials: 'same-origin', body: file,
      headers: { 'Content-Type': 'application/zip', 'X-Backup-Password': b64(f.pw.value) } });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) { showLogin(); return; }
    if (!r.ok) {
      const d = j.detail && typeof j.detail === 'object' && !Array.isArray(j.detail) ? j.detail : { message: j.detail };
      if (d.code === 'password') f.pw.focus();
      throw new Error(typeof d.message === 'string' ? d.message : r.status === 413 ? 'file troppo grande' : r.statusText);
    }
    $('toast').style.display = 'none';   // un errore di prima (es. password mancante) non resta a schermo
    showRestore(j, file.name);
  } catch (err) { toast('Errore: ' + err.message, true); }
  finally { btn.disabled = false; btn.textContent = 'Controlla il backup'; }
};
function showRestore(c, name) {
  rst = { ...(rst || {}), check: c };
  $('rst-result').hidden = false;
  $('rst-title').textContent = `${name}: backup ${c.backup_ts ? 'del ' + fmtWhen(c.backup_ts) : 'di MeshWatch'}${c.encrypted ? ' · 🔒 protetto da password' : ''}`;
  const cells = [];
  if (c.has_data) {
    const d = c.data;
    cells.push(cell('Dispositivi', d.trackers.length ? d.trackers.join(', ') : 'nessuno'),
      cell('Ripetitori', d.repeaters.length ? d.repeaters.join(', ') : 'nessuno'),
      cell('Utenti', d.users.map(u => `${u.username} (${u.role})`).join(', ')),
      cell('Posizioni', `${d.positions}${d.first_ts ? ` dal ${fmtWhen(d.first_ts).slice(0, 10)} al ${fmtWhen(d.last_ts).slice(0, 10)}` : ''}`),
      cell('Messaggi', String(d.messages)), cell('Database', fmtSize(d.size)));
  }
  if (c.has_config) {
    const k = c.config;
    cells.push(cell('Telegram', k.telegram ? `bot configurato, ${k.chats} chat` : 'non configurato'),
      cell('Radio', k.radio), cell('Pagina web', k.web_mode));
  }
  $('rst-info').replaceChildren(...cells);
  const warns = [...(c.warnings || []), ...(c.config_warnings || []).map(w => 'Configurazione: ' + w)];
  $('rst-warn').replaceChildren(...warns.map(w => el('p', { className: 'note warn', textContent: '⚠️ ' + w })));
  const f = $('form-rst-apply');
  f.data.checked = c.has_data; f.data.disabled = !c.has_data;
  const cfgOk = c.has_config && !!c.config_mode;
  f.config.checked = false; f.config.disabled = !cfgOk;
  $('rst-cfg-note').textContent = !c.has_config ? 'il backup non contiene il config'
    : cfgOk ? 'token Telegram, radio, pagina web (config.yaml)' : 'non disponibile: ' + c.config_reason;
  $('rst-result').scrollIntoView({ behavior: 'smooth', block: 'start' });
}
$('rst-cancel').onclick = () => { $('rst-result').hidden = true; $('form-rst').reset(); rst.check = null; };
$('form-rst-apply').onsubmit = async e => {
  e.preventDefault(); const f = e.target, c = rst && rst.check;
  if (!c) return;
  const what = [f.data.checked && 'i dati', f.config.checked && 'la configurazione'].filter(Boolean);
  if (!what.length) { toast('Scegli cosa ripristinare', true); return; }
  if (!confirm(`Ripristinare ${what.join(' e ')} dal backup${c.backup_ts ? ' del ' + fmtWhen(c.backup_ts) : ''}?\n\n`
    + 'Lo stato attuale viene salvato (si può annullare). Il servizio si riavvia: per circa un minuto pagina e bot non rispondono.'
    + (f.data.checked ? '\nDopo il riavvio si entra con utenti e password del backup.' : ''))) return;
  const btn = $('rst-apply'); btn.disabled = true;
  try {
    await post('/api/restore/apply', { token: c.token, data: f.data.checked, config: f.config.checked });
    $('rst-result').hidden = true;
    waitBack($('rst-log'), 'Ripristino in corso: il servizio si riavvia, la pagina si ricarica da sola…');
  } catch (err) { if (err.message !== 'login') toast('Errore: ' + err.message, true); btn.disabled = false; }
};
$('rst-undo').onclick = async e => {
  if (!confirm('Annullare l\'ultimo ripristino e rimettere lo stato precedente?\nIl servizio si riavvia (circa un minuto).')) return;
  e.target.disabled = true;
  try { await post('/api/restore/undo'); waitBack($('rst-log'), 'Rimetto lo stato precedente: il servizio si riavvia…'); }
  catch (err) { if (err.message !== 'login') toast('Errore: ' + err.message, true); e.target.disabled = false; }
};
async function waitBack(box, msg) {
  box.hidden = false; box.textContent = msg;
  const t0 = Date.now();
  await new Promise(r => setTimeout(r, 4000));
  while (Date.now() - t0 < 10 * 60000) {
    try { const r = await fetch('/api/me', { cache: 'no-store' }); if (r.ok || r.status === 401) { location.reload(); return; } } catch (e) { }
    await new Promise(r => setTimeout(r, 3000));
  }
  box.textContent = 'Il servizio non risponde ancora: ricarica la pagina tra qualche minuto.';
}

// ---------- riepilogo giornaliero su Telegram
async function loadReport() {
  let r; try { r = await api('/api/telegram/report'); } catch (e) { return; }
  const f = $('form-report');
  f.enabled.checked = r.enabled; f.time.value = r.time;
  [f.enabled, f.time, f.querySelector('.primary'), $('rep-send')].forEach(x => x.disabled = !r.telegram);
  $('rep-off').hidden = r.telegram;
  $('rep-state').textContent = !r.telegram ? '' : r.enabled
    ? `Attivo: arriva ogni giorno alle ${r.time} a tutte le chat autorizzate.` : 'Spento: il riepilogo arriva solo se lo chiedi (pulsante 📝 Riepilogo del bot).';
}
$('form-report').onsubmit = e => {
  e.preventDefault(); const f = e.target;
  action(f.querySelector('.primary'), () => put('/api/telegram/report', { enabled: f.enabled.checked, time: f.time.value }),
    r => r.enabled ? `Riepilogo ogni giorno alle ${r.time}` : 'Riepilogo automatico spento').then(loadReport);
};
$('rep-send').onclick = e => action(e.target, () => post('/api/telegram/report/send'), 'Riepilogo inviato su Telegram');
$('mnt-purge').onclick = e => {
  if (!confirm(`Cancellare adesso i dati più vecchi di ${$('form-mnt').retention_days.value} giorni?`)) return;
  action(e.target, () => post('/api/maintenance/purge'), r => r.deleted ? `Eliminate ${r.deleted} righe` : 'Niente da cancellare').then(loadMaintenance);
};
$('mnt-telegram').onclick = e => action(e.target, () => post('/api/backup/telegram'), 'Backup inviato').then(loadMaintenance);
$('mnt-download').onclick = () => setTimeout(loadAdmin, 1500);   // il registro mostra il download

// ---------- avvisi batteria bassa
async function loadBatteryAlerts() {
  let b; try { b = await api('/api/alerts/battery'); } catch (e) { return; }
  const f = $('form-batt');
  const v = String(b.batt_repeat_h);
  if (![...f.batt_repeat_h.options].some(o => o.value === v)) f.batt_repeat_h.append(el('option', { value: v, textContent: `${v} ore` }));
  f.batt_repeat_h.value = v;
  f.quiet_on.checked = !!b.batt_quiet;
  if (b.batt_quiet) [f.q_from.value, f.q_to.value] = b.batt_quiet.split('-');
}
$('form-batt').onsubmit = e => {
  e.preventDefault(); const f = e.target;
  action(f.querySelector('.primary'), () => {
    if (f.quiet_on.checked && (!f.q_from.value || !f.q_to.value || f.q_from.value === f.q_to.value))
      throw new Error('scegli due orari diversi per il silenzio');
    return put('/api/alerts/battery', { batt_repeat_h: +f.batt_repeat_h.value,
      batt_quiet: f.quiet_on.checked ? `${f.q_from.value}-${f.q_to.value}` : '' });
  }, 'Impostazioni salvate').then(loadBatteryAlerts);
};

// ---------- cache delle mappe
async function loadTileCache() {
  let c; try { c = await api('/api/tilecache'); } catch (e) { return; }
  const asked = c.hits + c.misses;
  $('tc-info').replaceChildren(
    cell('Stato', c.enabled ? 'attiva' : 'disattivata'), cell('Tessere salvate', String(c.tiles)),
    cell('Spazio usato', `${fmtSize(c.size)} di ${c.max_mb} MB`), cell('Disco libero', fmtSize(c.disk_free)),
    cell('Dalla cache', asked ? `${Math.round(c.hits * 100 / asked)}% (dall'avvio)` : '—'),
    cell('Non scaricate', c.errors ? `${c.errors}${c.stale ? `, ${c.stale} vecchie mostrate` : ''}` : '0'));
  const f = $('form-tc');
  f.enabled.checked = c.enabled; f.max_mb.value = c.max_mb;
  $('tc-clear').disabled = !c.tiles;
  useTileCache(c.enabled);
}
$('form-tc').onsubmit = e => {
  e.preventDefault(); const f = e.target;
  action(f.querySelector('.primary'), () => put('/api/tilecache', { enabled: f.enabled.checked, max_mb: +f.max_mb.value }), 'Impostazioni salvate')
    .then(loadTileCache);
};
$('tc-clear').onclick = e => {
  if (!confirm('Cancellare tutte le tessere salvate sul Raspberry?\nLa mappa le riscaricherà man mano.')) return;
  action(e.target, () => post('/api/tilecache/clear'), r => `Cancellate ${r.deleted} tessere`).then(loadTileCache);
};
$('tc-reload').onclick = () => loadTileCache();

$('sys-radio').onclick = e => action(e.target, () => post('/api/radio/reconnect'), 'Riconnessione avviata: ci vuole qualche secondo');

$('add-tracker').onclick = () => trackerForm(null);
$('add-rep').onclick = () => repForm(null);
$('add-user').onclick = () => userForm(null);

// ---------- rubrica del gateway
let ctc = null;
async function loadContacts() {
  try { ctc = await api('/api/contacts'); }
  catch (e) { if (e.message !== 'login') $('ctc-list').replaceChildren(el('p', { className: 'muted', textContent: 'Rubrica non disponibile: ' + e.message })); return; }
  $('ctc-manual').checked = ctc.manual_add;
  renderContacts();
}
function renderContacts() {
  if (!ctc) return;
  const q = $('ctc-filter').value.trim().toLowerCase(), type = $('ctc-type').value;
  const list = ctc.contacts.filter(c => (!type || c.type === type) && (!q || c.name.toLowerCase().includes(q) || c.public_key.startsWith(q)))
    .sort((a, b) => (b.favorite - a.favorite) || (b.last_advert || 0) - (a.last_advert || 0));
  const box = $('ctc-list'); box.replaceChildren();
  if (!list.length) box.append(el('p', { className: 'muted', textContent: ctc.contacts.length ? 'Nessun contatto corrisponde alla ricerca.' : 'La rubrica del gateway è vuota.' }));
  list.forEach(c => {
    const enc_ = encodeURIComponent(c.public_key);
    const fav = el('button', { className: 'small', textContent: c.favorite ? '★ Preferito' : '☆ Preferito', title: c.favorite ? 'Togli dai preferiti' : 'Aggiungi ai preferiti' });
    fav.onclick = () => action(fav, () => post(`/api/contacts/${enc_}/favorite`, { on: !c.favorite }), c.favorite ? 'Tolto dai preferiti' : 'Aggiunto ai preferiti').then(loadContacts);
    const path = el('button', { className: 'small', textContent: 'Azzera percorso' });
    path.onclick = () => action(path, () => post(`/api/contacts/${enc_}/reset_path`), 'Percorso azzerato: il prossimo invio cerca la strada in flood').then(loadContacts);
    const rm = el('button', { className: 'small danger', textContent: 'Rimuovi' });
    rm.onclick = async () => {
      const warn = c.used_by.length ? `\nATTENZIONE: è usato da ${c.used_by.join(', ')}: smetterà di funzionare finché il nodo non viene riaggiunto.` : '';
      if (!confirm(`Rimuovere ${c.name} dalla rubrica del gateway?${warn}\nSe il nodo manda un nuovo advert può tornare in rubrica.`)) return;
      action(rm, () => del(`/api/contacts/${enc_}` + (c.used_by.length ? '?force=true' : '')), 'Contatto rimosso').then(loadContacts);
    };
    const acts = el('span', { className: 'acts' }, fav, path);
    if (['companion', 'room server'].includes(c.type)) { const w = el('button', { className: 'small', textContent: 'Scrivi' }); w.onclick = () => writeTo(c); acts.append(w); }
    acts.append(rm);
    const info = [c.type, hopsText(c.hops) + (c.route && c.route.length ? ' via ' + c.route.join(' → ') : ''), 'advert ' + (c.last_advert ? fmtAgo(c.last_advert) : 'mai'),
      c.lat != null ? `${c.lat.toFixed(4)}, ${c.lon.toFixed(4)}` : 'senza posizione', c.public_key.slice(0, 12)];
    if (c.used_by.length) info.push('usato da ' + c.used_by.join(', '));
    box.append(el('div', { className: 'item' },
      el('span', { className: 'name' }, c.favorite ? el('span', { className: 'star', textContent: '★' }) : null, c.name),
      el('span', { className: 'info mono', textContent: info.join(' · ') }), acts));
  });
  const pend = ctc.pending || [];
  $('ctc-pending-box').hidden = !pend.length;
  $('ctc-pending').replaceChildren(...pend.map(c => {
    const add = el('button', { className: 'small primary', textContent: 'Aggiungi' });
    add.onclick = () => action(add, () => post('/api/contacts/pending/' + encodeURIComponent(c.public_key)), `${c.name} aggiunto`).then(loadContacts);
    return el('div', { className: 'item' }, el('span', { className: 'name', textContent: c.name }),
      el('span', { className: 'info mono', textContent: `${c.type} · ${c.public_key.slice(0, 12)}` }), el('span', { className: 'acts' }, add));
  }));
}
$('ctc-filter').oninput = renderContacts;
$('ctc-type').onchange = renderContacts;
$('ctc-refresh').onclick = e => action(e.target, () => post('/api/contacts/refresh'), 'Rubrica riletta').then(loadContacts);
$('ctc-nb').onclick = e => action(e.target, () => post('/api/network/neighbours'),
  r => { const v = Object.values(r.result); return v.length ? `Vicini letti da ${v.filter(Boolean).length} ripetitori su ${v.length}` : 'Nessun ripetitore configurato'; }).then(loadNetwork);
$('ctc-manual').onchange = e => {
  const on = e.target.checked;
  action(null, () => put('/api/contacts/settings', { manual_add: on }), on ? 'Aggiunta manuale attiva' : 'Aggiunta automatica attiva').then(loadContacts);
};
