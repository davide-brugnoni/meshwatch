// MeshWatch — colonna dei nodi: dispositivi, ripetitori, terminale
// ---------- vista mappa
// grafici dei ripetitori: una metrica scelta per tutti (ricordata nel browser)
const METRICS = {
  bat: { label: 'Batteria', unit: 'V', dec: 2, get: h => h.bat_mv ? h.bat_mv / 1000 : null, pad: 0.05, color: '#2fb38a' },
  noise: { label: 'Rumore', unit: 'dBm', dec: 0, get: h => h.noise, pad: 1, color: '#e8c547' },
  snr: { label: 'SNR', unit: 'dB', dec: 1, get: h => h.snr, pad: 0.5, color: '#5aa9ff' },
  rssi: { label: 'RSSI', unit: 'dBm', dec: 0, get: h => h.rssi, pad: 1, color: '#c77dff' },
};
let repMetric = 'bat';
try { if (METRICS[localStorage.getItem('repMetric')]) repMetric = localStorage.getItem('repMetric'); } catch (e) { }
function metricTabs() {
  return el('div', { className: 'metric-tabs' }, ...Object.entries(METRICS).map(([k, m]) => {
    const b = el('button', { className: k === repMetric ? 'on' : '', textContent: m.label });
    b.onclick = () => { repMetric = k; try { localStorage.setItem('repMetric', k); } catch (e) { } render(); };
    return b;
  }));
}
function metricChart(history) {
  const m = METRICS[repMetric];
  const pts = history.map(h => [h.ts, m.get(h)]).filter(p => p[1] != null);
  if (pts.length < 2) return el('p', { className: 'muted', style: 'margin:6px 0 0;font-size:12.5px', textContent: `${m.label}: pochi dati nel periodo` });
  const vals = pts.map(p => p[1]), lo = Math.min(...vals), hi = Math.max(...vals);
  const f = v => v.toFixed(m.dec) + ' ' + m.unit;
  return el('div', {}, spark(pts, m.color, m.pad, `${m.label} ${f(lo)} – ${f(hi)}`),
    el('div', { className: 'spark-range' }, el('span', { textContent: `min ${f(lo)}` }), el('span', { textContent: `ultimo ${f(vals.at(-1))}` }), el('span', { textContent: `max ${f(hi)}` })));
}
function spark(pts, color, pad, label) {
  if (pts.length < 2) return null;
  const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('class', 'spark'); svg.setAttribute('viewBox', '0 0 300 34'); svg.setAttribute('preserveAspectRatio', 'none');
  const t0 = pts[0][0], t1 = pts.at(-1)[0] || t0 + 1;
  let vmin = Math.min(...pts.map(p => p[1])), vmax = Math.max(...pts.map(p => p[1]));
  if (vmax - vmin < pad * 2) { vmin -= pad; vmax += pad; }
  const d = pts.map(([t, v], i) => `${i ? 'L' : 'M'}${((t - t0) / (t1 - t0 || 1) * 298 + 1).toFixed(1)},${(32 - (v - vmin) / (vmax - vmin) * 30).toFixed(1)}`).join('');
  const path = document.createElementNS(ns, 'path');
  path.setAttribute('d', d); path.setAttribute('fill', 'none'); path.setAttribute('stroke', color || '#2fb38a'); path.setAttribute('stroke-width', '1.6');
  path.setAttribute('vector-effect', 'non-scaling-stroke');
  const title = document.createElementNS(ns, 'title'); title.textContent = label;
  svg.append(title, path); return svg;
}
const cell = (label, value) => el('div', {}, el('span', { textContent: label }), value);

function trackerCard(t) {
  const p = t.last_position;
  const meta = [];
  if (p && p.batt_v != null) meta.push(`batteria ${p.batt_v.toFixed(2)} V`);
  if (p && p.alt != null) meta.push(`quota ${Math.round(p.alt)} m`);
  meta.push(`ultima risposta ${fmtTs(t.last_ok)}`);
  meta.push(t.paused ? 'IN PAUSA' : (t.moving && t.current_interval_min < t.interval_min ? `in movimento, ogni ${t.current_interval_min} min` : `ogni ${t.interval_min} min`)
    + (t.active_hours ? ' (' + t.active_hours + ')' : ''));
  const poll = el('button', { className: 'primary', textContent: 'Chiedi posizione' });
  poll.onclick = () => action(poll, () => post(`/api/trackers/${t.id}/poll`),
    r => r.ok ? (r.result.lat != null ? `Posizione di ${t.label} ricevuta` : 'Risposta ricevuta, GPS senza fix') : `${t.label} non ha risposto`);
  const row = el('div', { className: 'row' }, me && me.role !== 'guest' ? poll : null);
  if (p) {
    row.append(el('a', { className: 'btn', href: mapsUrl(p.lat, p.lon), target: '_blank', rel: 'noopener', textContent: 'Google Maps' }));
    const c = el('button', { textContent: 'Centra' }); c.onclick = () => focusTracker(t); row.append(c);
  }
  const iv = el('input', { type: 'number', min: 5, max: 720, value: t.interval_min });
  const save = el('button', { textContent: 'Salva' });
  save.onclick = () => action(save, () => put(`/api/trackers/${t.id}`, { interval_min: +iv.value }), 'Intervallo salvato');
  const pause = el('button', { textContent: t.paused ? 'Riprendi' : 'Pausa' });
  pause.onclick = () => action(pause, () => put(`/api/trackers/${t.id}`, { paused: !t.paused }));
  const clr = el('button', { className: 'danger', textContent: 'Cancella traccia' });
  clr.onclick = () => clearTracks(t, clr);
  const admRow = el('div', { className: 'row admin-only' }, el('label', { className: 'muted' }, 'Ogni ', iv, ' min'), save, pause, clr);
  return el('div', { className: 'trk' },
    el('div', { className: 'rep-head' }, el('span', { className: 'rep-name' }, swatch(t.color), t.label), el('span', { className: 'muted', textContent: t.contact !== t.label ? t.contact : '' })),
    el('p', { className: 'big', textContent: p ? fmtTs(p.ts) : 'Nessuna posizione' }),
    el('p', { className: 'muted', style: 'margin:0', textContent: meta.join(' · ') }),
    row, admRow);
}

function render() {
  const s = state;
  $('radio').replaceChildren(dot(s.radio_connected ? 'ok' : 'ko'), s.radio_connected ? 'Radio collegata' : 'Radio NON collegata');

  // dispositivi
  const tbox = $('trackers'); tbox.replaceChildren();
  if (!s.trackers.length) tbox.append(el('p', { className: 'muted', style: 'margin:0', textContent: 'Nessun dispositivo. Aggiungilo da Gestione.' }));
  s.trackers.forEach(t => tbox.append(trackerCard(t)));

  // riepilogo sopra la mappa (telefono)
  $('map-summary').replaceChildren(...s.trackers.map(t => {
    const p = t.last_position, b = el('button', {}, swatch(t.color), t.label, el('small', { textContent: p ? fmtAgo(p.ts) : 'nessuna posizione' }));
    b.onclick = () => p ? focusTracker(t) : setTab('list');
    return b;
  }));

  // tracce
  trackLayer.clearLayers();
  const all = [];
  s.trackers.forEach(t => {
    const pts = t.track.map(q => [q.lat, q.lon]); all.push(...pts);
    if (pts.length > 1) L.polyline(pts, { color: t.color, weight: 3, opacity: .85 }).addTo(trackLayer);
    const list = t.track.length ? t.track : (t.last_position ? [t.last_position] : []);
    list.forEach((q, i) => {
      const last = i === list.length - 1;
      const m = L.circleMarker([q.lat, q.lon], { radius: last ? 8 : 4, color: t.color, fillColor: last ? '#fff' : t.color, fillOpacity: last ? .9 : .5, weight: last ? 3 : 1 })
        .bindPopup(() => el('div', {}, el('strong', { textContent: t.label }), el('br'),
          fmtTs(q.ts) + (q.batt_v ? ` · ${q.batt_v.toFixed(2)} V` : ''), el('br'),
          el('a', { href: mapsUrl(q.lat, q.lon), target: '_blank', rel: 'noopener', textContent: 'Google Maps' })))
        .addTo(trackLayer);
      if (last) lastMarkers[t.id] = m;
      if (last && !t.track.length) all.push([q.lat, q.lon]);
    });
  });
  if (!fitDone && all.length) { all.length > 1 ? map.fitBounds(all, { padding: [40, 40], maxZoom: 16 }) : map.setView(all[0], 15); fitDone = true; }

  // ripetitori
  const box = $('reps'); box.replaceChildren();
  if (!s.repeaters.length) box.append(el('p', { className: 'muted', style: 'margin:0', textContent: 'Nessun ripetitore. Aggiungilo da Gestione.' }));
  s.repeaters.forEach(r => {
    const item = el('div', { className: 'rep' },
      el('div', { className: 'rep-head' },
        el('span', { className: 'rep-name' }, dot(r.last_ok ? 'ok' : r.last_poll ? 'ko' : ''), r.name),
        el('span', { className: 'muted', textContent: r.status ? fmtTs(r.status.ts) : 'nessun dato' })));
    if (r.status) {
      const st = r.status;
      item.append(el('div', { className: 'grid mono' },
        cell('Batteria', st.bat_mv ? (st.bat_mv / 1000).toFixed(2) + ' V' : '—'), cell('Uptime', fmtUp(st.uptime)),
        cell('Rumore', (st.noise ?? '—') + ' dBm'), cell('SNR', st.snr != null ? (+st.snr).toFixed(1) + ' dB' : '—'),
        cell('RSSI', (st.rssi ?? '—') + ' dBm'), cell('Rx/Tx', `${st.nb_recv ?? 0}/${st.nb_sent ?? 0}`)));
      item.append(metricTabs(), metricChart(r.history));
    }
    box.append(item);
  });

  $('contacts').textContent = s.contacts.length ? s.contacts.map(c => `${c.name} (${c.type})`).join(' · ') : 'nessuno';
  fillTermNodes();
}

// cancella i punti della traccia (di un dispositivo o di tutti); resta l'ultima posizione
function clearTracks(t, btn) {
  const who = t ? `di ${t.label}` : 'di tutti i dispositivi';
  if (!confirm(`Cancellare la traccia ${who}?\nI punti vengono eliminati definitivamente; resta solo l'ultima posizione.`)) return;
  action(btn, () => del(t ? `/api/trackers/${t.id}/track` : '/api/track'),
    r => r.deleted ? `Traccia cancellata (${r.deleted} punti)` : 'Nessun punto da cancellare')
    .then(() => { fitDone = false; refresh(); });
}

$('btn-reps').onclick = e => action(e.target, () => post('/api/repeaters/poll'), 'Ripetitori interrogati');
$('btn-advert').onclick = e => action(e.target, () => post('/api/advert'), 'Advert inviato');
$('range').onchange = () => { fitDone = false; refresh(); };

// ---------- terminale
const DANGER = ['reboot', 'set ', 'password', 'erase', 'start ota', 'clear', 'setperm', 'gps sync', 'time ', 'clock sync', 'region '];
const isDanger = c => { c = c.trim().toLowerCase(); return DANGER.some(p => c === p.trim() || c.startsWith(p)); };
const hist = []; let histPos = 0, termBusy = false;
function termLine(text, cls, ts) {
  const out = $('term-out'), line = el('div');
  if (ts) line.append(el('span', { className: 'ts', textContent: new Date(ts * 1000).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' }) + ' ' }));
  line.append(el('span', { className: cls || '', textContent: text }));
  out.append(line); out.scrollTop = out.scrollHeight;
}
function fillTermNodes() {
  if (!me || me.role !== 'admin') return;
  const sel = $('term-node'), names = state.repeaters.map(r => r.name);
  const cur = [...sel.options].map(o => o.value);
  if (names.join('|') === cur.join('|')) return;
  const keep = sel.value; sel.replaceChildren(...names.map(n => el('option', { value: n, textContent: n })));
  if (names.includes(keep)) sel.value = keep; else loadTermHistory();
}
async function loadTermHistory() {
  $('term-out').replaceChildren();
  if (!$('term-node').value) return termLine('Nessun ripetitore configurato.', 'ts');
  try {
    const rows = await api('/api/cli/' + encodeURIComponent($('term-node').value));
    rows.forEach(r => { termLine('> ' + r.cmd, 'cmd', r.ts); termLine(r.reply ?? '(nessuna risposta)', r.reply == null ? 'err' : ''); });
    if (!rows.length) termLine('Nessun comando inviato a questo ripetitore.', 'ts');
  } catch (e) { }
}
async function sendCmd(cmd) {
  cmd = cmd.trim(); const node = $('term-node').value;
  if (!cmd || !node || termBusy) return;
  let confirm = false;
  if (isDanger(cmd)) { if (!window.confirm(`Inviare "${cmd}" a ${node}?\nÈ un comando che modifica o riavvia il ripetitore.`)) return; confirm = true; }
  termBusy = true; $('term-send').disabled = true;
  hist.push(cmd); histPos = hist.length; $('term-cmd').value = '';
  termLine('> ' + cmd, 'cmd', Date.now() / 1000);
  const wait = el('div', { className: 'ts', textContent: '… in attesa della risposta via radio' }); $('term-out').append(wait);
  try {
    const r = await post('/api/cli', { node, cmd, confirm });
    wait.remove(); termLine(r.ok ? r.reply : '(nessuna risposta: ripetitore non raggiungibile o password errata)', r.ok ? '' : 'err');
  } catch (e) { wait.remove(); if (e.message !== 'login') termLine('Errore: ' + e.message, 'err'); }
  termBusy = false; $('term-send').disabled = false; $('term-cmd').focus();
}
$('term-form').onsubmit = e => { e.preventDefault(); sendCmd($('term-cmd').value); };
$('term-node').onchange = loadTermHistory;
document.querySelectorAll('.chips button').forEach(b => b.onclick = () => sendCmd(b.dataset.c));
$('term-cmd').onkeydown = e => {
  if (e.key === 'ArrowUp' && histPos > 0) { histPos--; $('term-cmd').value = hist[histPos]; e.preventDefault(); }
  if (e.key === 'ArrowDown') { histPos = Math.min(hist.length, histPos + 1); $('term-cmd').value = hist[histPos] ?? ''; e.preventDefault(); }
};
