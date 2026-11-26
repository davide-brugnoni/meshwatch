// MeshWatch — scheda Statistiche: andamento della rete, nodi, disponibilità, confronto,
// batteria e collegamenti dei ripetitori. Grafici in SVG disegnati alla larghezza reale.
const SVGNS = 'http://www.w3.org/2000/svg';
const sv = (tag, attrs = {}, ...kids) => {
  const e = document.createElementNS(SVGNS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  e.append(...kids.filter(k => k != null));
  return e;
};
const fmtN = (v, dec = 0) => v == null ? '—' : (+v).toLocaleString('it-IT', { minimumFractionDigits: dec, maximumFractionDigits: dec });
const stDay = d => { const [, m, g] = d.split('-'); return `${g}/${m}`; };
const fmtHour = ts => new Date(ts * 1000).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' });
const dayLab = ts => { const d = new Date(ts * 1000); return `${String(d.getDate()).padStart(2, '0')}/${String(d.getMonth() + 1).padStart(2, '0')}`; };
const fmtDate = ts => ts ? new Date(ts * 1000).toLocaleDateString('it-IT', { day: '2-digit', month: '2-digit', year: 'numeric' }) : '—';
const WEEKDAYS = ['Lun', 'Mar', 'Mer', 'Gio', 'Ven', 'Sab', 'Dom'];
const WEEKDAYS_FULL = ['lunedì', 'martedì', 'mercoledì', 'giovedì', 'venerdì', 'sabato', 'domenica'];
// rampa sequenziale (un solo blu, dal vicino allo sfondo al più chiaro) e colori di stato
const RAMP = ['#0e3d7e', '#1757ac', '#2370cd', '#338ced', '#6aabf1', '#9dc8f7'];
const ST_COLORS = { good: '#0aac23', warn: '#fbb315', serious: '#f18a59', crit: '#dc3c3d', none: '#262c30' };

let stats = null, statsAt = 0, statsTimer = null, statsBusy = false;
const stView = { nodeFilter: 'all', nodeSort: { key: 'last_seen', asc: false }, cmpSort: { key: 'name', asc: true },
  nodesAll: false, linksAll: false };

// ---------- tooltip unico per tutti i grafici (mouse e dito)
const tipEl = $('st-tip');
function moveTip(e) {
  const pad = 14, w = tipEl.offsetWidth, h = tipEl.offsetHeight;
  let x = e.clientX + pad, y = e.clientY + pad;
  if (x + w > innerWidth - 8) x = Math.max(8, e.clientX - w - pad);
  if (y + h > innerHeight - 8) y = Math.max(8, e.clientY - h - pad);
  tipEl.style.left = x + 'px'; tipEl.style.top = y + 'px';
}
function showTip(e, lines) {
  if (!lines) return hideTip();
  const [head, ...rest] = Array.isArray(lines) ? lines : [lines];
  tipEl.replaceChildren(el('b', { textContent: head }), rest.length ? '\n' + rest.join('\n') : '');
  tipEl.hidden = false; moveTip(e);
}
function hideTip() { tipEl.hidden = true; }
function bindTip(node, fn) {
  node.addEventListener('pointerenter', e => showTip(e, fn(e)));
  node.addEventListener('pointermove', e => { if (!tipEl.hidden) moveTip(e); });
  node.addEventListener('pointerleave', e => { if (e.pointerType === 'mouse') hideTip(); });
  node._tipped = true;
}
document.addEventListener('pointerdown', e => { if (!e.target._tipped) hideTip(); }, true);
$('stats-scroll').addEventListener('scroll', hideTip, { passive: true });

// ---------- grafici: disegnati quando il contenitore è nella pagina, ridisegnati se cambia la larghezza
const charts = [];
function chartBox(draw, cls = '') {
  const box = el('div', { className: 'st-plot ' + cls });
  box._draw = draw; charts.push(box);
  return box;
}
function drawCharts() {
  for (let i = charts.length - 1; i >= 0; i--) {
    const b = charts[i];
    if (!b.isConnected) { charts.splice(i, 1); continue; }
    const w = Math.round(b.clientWidth);
    if (!w || w === b._w) continue;
    b._w = w; b.replaceChildren(b._draw(w));
  }
}
let resizeT = null;
addEventListener('resize', () => { clearTimeout(resizeT); resizeT = setTimeout(drawCharts, 150); });

function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v))), f = v / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
}
// colonne verticali: una per giorno o per ora
function barChart(items, { value, tip, xlab, h = 130, fmt = v => fmtN(v) }) {
  return w => {
    const padL = 36, padR = 4, padT = 8, padB = 18, pw = w - padL - padR, ph = h - padT - padB;
    const vals = items.map(value), max = niceMax(Math.max(0, ...vals.map(v => v || 0)));
    const svg = sv('svg', { class: 'st-svg', width: w, height: h, viewBox: `0 0 ${w} ${h}` });
    for (const f of [0, 0.5, 1]) {
      const y = padT + ph - f * ph;
      svg.append(sv('line', { class: f ? 'grid-l' : 'base-l', x1: padL, x2: w - padR, y1: y, y2: y }),
        sv('text', { x: padL - 5, y: y + 3.5, 'text-anchor': 'end' }, fmt(max * f)));
    }
    const step = pw / items.length, bw = Math.max(1, step - 2);
    items.forEach((it, i) => {
      const x = padL + i * step + (step - bw) / 2, v = vals[i] || 0, bh = v / max * ph, y0 = padT + ph;
      const hit = sv('rect', { class: 'hit', x: padL + i * step, y: padT, width: step, height: ph });
      bindTip(hit, () => tip(it));
      svg.append(hit);
      if (bh > 0) {
        const r = Math.min(4, bw / 2, bh), y = y0 - bh;
        svg.append(sv('path', { class: 'bar', d: `M${x},${y0}V${y + r}Q${x},${y} ${x + r},${y}H${x + bw - r}Q${x + bw},${y} ${x + bw},${y + r}V${y0}Z` }));
      } else svg.append(sv('g'));   // segnaposto: l'evidenziazione al passaggio resta allineata
      const lab = xlab(it, i);
      if (lab) svg.append(sv('text', { x: padL + i * step + step / 2, y: h - 4, 'text-anchor': 'middle' }, lab));
    });
    return svg;
  };
}
// linea nel tempo con mirino: pts = [[ts, valore]]
function lineChart(pts, { color = '#338ced', h = 120, fmt = v => fmtN(v, 1), thr = null, thrLabel = '', label = '' }) {
  return w => {
    if (pts.length < 2) return el('p', { className: 'muted', textContent: 'Pochi dati nel periodo.' });
    const padL = 44, padR = 6, padT = 8, padB = 18, pw = w - padL - padR, ph = h - padT - padB;
    const t0 = pts[0][0], t1 = pts.at(-1)[0];
    const vs = pts.map(p => p[1]).concat(thr != null ? [thr] : []);
    let lo = Math.min(...vs), hi = Math.max(...vs);
    if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
    const padV = (hi - lo) * 0.08; lo -= padV; hi += padV;
    const X = t => padL + (t - t0) / (t1 - t0 || 1) * pw, Y = v => padT + ph - (v - lo) / (hi - lo) * ph;
    const svg = sv('svg', { class: 'st-svg', width: w, height: h, viewBox: `0 0 ${w} ${h}` });
    for (const f of [0, 0.5, 1]) {
      const v = lo + (hi - lo) * f, y = Y(v);
      svg.append(sv('line', { class: f ? 'grid-l' : 'base-l', x1: padL, x2: w - padR, y1: y, y2: y }),
        sv('text', { x: padL - 5, y: y + 3.5, 'text-anchor': 'end' }, fmt(v)));
    }
    const tlab = t1 - t0 > 2 * 86400 ? dayLab : fmtHour;
    [[t0, 'start'], [(t0 + t1) / 2, 'middle'], [t1, 'end']].forEach(([t, a]) =>
      svg.append(sv('text', { x: X(t), y: h - 4, 'text-anchor': a }, tlab(t))));
    if (thr != null) svg.append(sv('line', { class: 'thr', x1: padL, x2: w - padR, y1: Y(thr), y2: Y(thr) }),
      sv('text', { x: w - padR, y: Y(thr) - 4, 'text-anchor': 'end' }, thrLabel));
    svg.append(sv('path', { d: pts.map(([t, v], i) => `${i ? 'L' : 'M'}${X(t).toFixed(1)},${Y(v).toFixed(1)}`).join(''),
      fill: 'none', stroke: color, 'stroke-width': 2, 'stroke-linejoin': 'round' }));
    const cross = sv('line', { class: 'cross', y1: padT, y2: padT + ph, visibility: 'hidden' });
    const dotM = sv('circle', { r: 4, fill: color, stroke: '#171b1e', 'stroke-width': 2, visibility: 'hidden' });
    const hit = sv('rect', { class: 'hit', x: padL, y: padT, width: pw, height: ph });
    const near = e => {
      const r = svg.getBoundingClientRect(), x = e.clientX - r.left;
      let best = pts[0];
      for (const p of pts) if (Math.abs(X(p[0]) - x) < Math.abs(X(best[0]) - x)) best = p;
      cross.setAttribute('x1', X(best[0])); cross.setAttribute('x2', X(best[0]));
      dotM.setAttribute('cx', X(best[0])); dotM.setAttribute('cy', Y(best[1]));
      cross.setAttribute('visibility', 'visible'); dotM.setAttribute('visibility', 'visible');
      return best;
    };
    const txt = p => [`${label}${fmt(p[1])}`, `${dayLab(p[0])} ${fmtHour(p[0])}`];
    hit.addEventListener('pointerenter', e => showTip(e, txt(near(e))));
    hit.addEventListener('pointermove', e => showTip(e, txt(near(e))));
    hit.addEventListener('pointerleave', e => { if (e.pointerType === 'mouse') { hideTip(); cross.setAttribute('visibility', 'hidden'); dotM.setAttribute('visibility', 'hidden'); } });
    hit._tipped = true;
    svg.append(cross, dotM, hit);
    return svg;
  };
}
function heatmap(heat, unit) {
  const vals = heat.flat().filter(v => v != null), max = Math.max(0, ...vals);
  const color = v => RAMP[Math.min(RAMP.length - 1, Math.floor(v / (max || 1) * RAMP.length))];
  const grid = el('div', { className: 'st-heat' }, el('span'));
  for (let hr = 0; hr < 24; hr++) grid.append(el('span', { className: 'hr', textContent: hr % 6 === 0 ? String(hr) : '' }));
  heat.forEach((row, d) => {
    grid.append(el('span', { className: 'lbl', textContent: WEEKDAYS[d] }));
    row.forEach((v, hr) => {
      const c = el('i', { className: v != null ? 'v' : '' });
      if (v != null) c.style.background = color(v);
      bindTip(c, () => [`${WEEKDAYS_FULL[d]}, ${String(hr).padStart(2, '0')}:00–${String(hr + 1).padStart(2, '0')}:00`,
        v == null ? 'nessun dato' : `${fmtN(v, v < 10 ? 1 : 0)} ${unit} all'ora in media`]);
      grid.append(c);
    });
  });
  const legend = el('div', { className: 'st-legend' }, el('span', { textContent: '0' }),
    el('span', { className: 'ramp' }, ...RAMP.map(c => { const i = el('i'); i.style.background = c; return i; })),
    el('span', { textContent: `${fmtN(max, max < 10 ? 1 : 0)} ${unit}/ora` }));
  return el('div', {}, grid, legend);
}
function hbars(items) {
  const max = Math.max(1, ...items.map(i => i.n)), tot = items.reduce((a, b) => a + b.n, 0);
  return el('div', { className: 'st-hbars' }, ...items.map(i => {
    const f = el('div', { className: 'fill' }); f.style.width = (i.n / max * 100).toFixed(1) + '%';
    const row = el('div', {}, el('span', { textContent: i.label }), el('div', { className: 'track' }, f),
      el('span', { className: 'n', textContent: fmtN(i.n) }));
    bindTip(row, () => [i.label, `${fmtN(i.n)} pacchetti · ${fmtN(i.n / tot * 100, 1)}% del totale`]);
    return row;
  }));
}
const stChart = (title, sub, body) => el('div', { className: 'st-chart' }, el('h3', { textContent: title }),
  sub ? el('p', { className: 'muted', textContent: sub }) : null, body);

// ---------- tabella ordinabile; col.worse = 'high' | 'low' segna il valore peggiore della colonna
function sortTable(cols, rows, sort, onSort) {
  const col = cols.find(c => c.k === sort.key) || cols[0];
  const get = c => r => c.sortv ? c.sortv(r) : r[c.k];
  const g = get(col);
  const sorted = rows.slice().sort((a, b) => {
    const x = g(a), y = g(b);
    if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1;
    const d = typeof x === 'string' ? x.localeCompare(y, 'it', { sensitivity: 'base' }) : x - y;
    return sort.asc ? d : -d;
  });
  const worst = {};
  for (const c of cols) {
    if (!c.worse) continue;
    const vs = rows.map(r => r[c.k]).filter(v => v != null);
    if (vs.length < 2) continue;
    const w = c.worse === 'high' ? Math.max(...vs) : Math.min(...vs);
    if (w !== (c.worse === 'high' ? Math.min(...vs) : Math.max(...vs))) worst[c.k] = w;
  }
  const head = el('tr', {}, ...cols.map(c => {
    const th = el('th', { textContent: c.t, className: (c.num ? 'num ' : '') + (c.k === col.k ? 'sorted' + (sort.asc ? ' asc' : '') : ''),
      title: c.title || 'Ordina' });
    th.onclick = () => { onSort(c.k === sort.key ? { key: c.k, asc: !sort.asc } : { key: c.k, asc: !!c.ascFirst }); };
    return th;
  }));
  const body = sorted.map(r => el('tr', {}, ...cols.map(c => {
    const v = r[c.k], td = el('td', { className: (c.num ? 'num ' : '') + (c.cls || '') });
    if (c.k in worst && v === worst[c.k]) { td.classList.add('worst'); td.title = 'Valore peggiore tra i ripetitori'; }
    const out = c.f ? c.f(r) : v == null ? '—' : String(v);
    td.append(out);
    return td;
  })));
  return el('table', { className: 'st-table' }, el('thead', {}, head), el('tbody', {}, ...body));
}

// ---------- sezioni
function renderKpi(s) {
  const avail = (stats.availability || []).map(a => a.pct_7d).filter(v => v != null);
  const kpi = (label, value, small) => el('div', {}, el('span', { textContent: label }), el('b', { textContent: value }),
    small ? el('small', { textContent: small }) : null);
  $('st-kpi').replaceChildren(
    kpi('Nodi attivi', fmtN(s.active_24h), `ultime 24 ore · ${fmtN(s.active_7d)} in 7 giorni`),
    kpi('Nodi conosciuti', fmtN(s.nodes), 'da quando è attivo lo storico'),
    kpi('Nuovi', fmtN(s.new_7d), 'negli ultimi 7 giorni'),
    kpi('Spariti', fmtN(s.gone), `zitti da più di ${s.gone_h} ore`),
    kpi('Advert', fmtN(s.adverts_24h), 'ricevuti nelle ultime 24 ore'),
    kpi('Pacchetti', s.packets_24h == null ? '—' : fmtN(s.packets_24h), s.packets_24h == null ? 'il firmware non li inoltra' : 'sentiti nelle ultime 24 ore'),
    kpi('Ripetitori', avail.length ? fmtN(avail.reduce((a, b) => a + b, 0) / avail.length, 1) + '%' : '—', 'disponibilità media in 7 giorni'));
  const parts = [];
  if (s.since) parts.push(`Storico dei nodi dal ${fmtDate(s.since)}.`);
  parts.push(`Aggiornato alle ${fmtHour(stats.generated)}.`);
  $('st-since').textContent = parts.join(' ');
}

function renderTrend(t) {
  const days = t.days, every = Math.ceil(days.length / 7);
  const hasPk = t.hours.some(h => h.packets), key = hasPk ? 'packets' : 'adverts';
  const items = [
    stChart('Nodi attivi al giorno', `Nodi diversi sentiti ogni giorno, ultimi ${stats.days} giorni`,
      chartBox(barChart(days, { value: d => d.nodes, xlab: (d, i) => (days.length - 1 - i) % every === 0 ? stDay(d.day) : null,
        tip: d => [stDay(d.day), `${fmtN(d.nodes)} nodi attivi`, `${fmtN(d.adverts)} advert`, d.new ? `${fmtN(d.new)} nuovi` : null].filter(Boolean) }))),
    stChart(hasPk ? 'Pacchetti all\'ora' : 'Advert all\'ora', hasPk ? 'Tutto quello che la radio ha sentito, ultime 48 ore' : 'Ultime 48 ore (i pacchetti non sono disponibili)',
      chartBox(barChart(t.hours, { value: h => h[key], xlab: (h, i) => new Date(h.hour * 1000).getHours() % 6 === 0 && i < 47 ? String(new Date(h.hour * 1000).getHours()).padStart(2, '0') : null,
        tip: h => { const e = new Date((h.hour + 3600) * 1000); return [`${dayLab(h.hour)} ${fmtHour(h.hour)}–${String(e.getHours()).padStart(2, '0')}:00`,
          hasPk ? `${fmtN(h.packets)} pacchetti` : null, `${fmtN(h.adverts)} advert`].filter(Boolean); } }))),
    stChart('Quando la rete è più trafficata', `Media per giorno della settimana e ora, ultimi ${stats.days} giorni`,
      t.heat.flat().some(v => v != null) ? heatmap(t.heat, t.heat_source === 'packets' ? 'pacchetti' : 'advert')
        : el('p', { className: 'muted', textContent: 'Ancora nessun dato.' })),
    stChart('Tipi di pacchetto', 'Ultime 24 ore',
      t.types.length ? hbars(t.types) : el('p', { className: 'muted', textContent: 'Nessun pacchetto contato: serve un firmware companion che inoltra i pacchetti ricevuti (1.7 o più recente).' })),
  ];
  $('st-trend').replaceChildren(el('div', { className: 'st-charts' }, ...items));
}

function nodeIsGone(n) { return n.last_seen < stats.generated - stats.summary.gone_h * 3600; }
function renderNodes() {
  const all = stats.nodes, now = stats.generated;
  const week = all.filter(n => n.last_seen >= now - 7 * 86400);
  const top = (list, val, fmt) => el('ol', {}, ...list.slice(0, 5).map(n => el('li', {}, n.name || n.key.slice(0, 8), el('span', { textContent: fmt(val(n)) }))));
  const rank = (title, list, val, fmt) => el('div', {}, el('div', { className: 'st-sub', textContent: title }),
    list.length ? top(list, val, fmt) : el('p', { className: 'muted', textContent: 'Ancora nessun dato.' }));
  $('st-rank').replaceChildren(
    rank('Più attivi (advert in 7 giorni)', week.filter(n => n.adverts_7d).sort((a, b) => b.adverts_7d - a.adverts_7d), n => n.adverts_7d, v => fmtN(v)),
    rank('Segnale migliore (SNR)', week.filter(n => n.snr != null).sort((a, b) => b.snr - a.snr), n => n.snr, v => fmtN(v, 1) + ' dB'),
    rank('Più lontani (salti)', week.filter(n => n.hops != null && n.hops > 0).sort((a, b) => b.hops - a.hops), n => n.hops, v => v + (v === 1 ? ' salto' : ' salti')));

  const q = $('st-nsearch').value.trim().toLowerCase(), type = $('st-ntype').value, f = stView.nodeFilter;
  const rows = all.filter(n => (!q || (n.name || '').toLowerCase().includes(q) || n.key.includes(q)) && (!type || n.type === type)
    && (f === 'all' || (f === 'new' ? n.new : nodeIsGone(n))));
  const cols = [
    { k: 'name', t: 'Nome', cls: 'name', ascFirst: true, sortv: n => n.name || n.key,
      f: n => el('span', { title: n.key }, n.name || n.key.slice(0, 8), n.new ? el('span', { className: 'st-tag new', textContent: 'nuovo' }) : null,
        nodeIsGone(n) ? el('span', { className: 'st-tag gone', textContent: 'sparito' }) : null) },
    { k: 'type', t: 'Tipo', ascFirst: true },
    { k: 'last_seen', t: 'Ultimo advert', f: n => el('span', { title: fmtTs(n.last_seen), textContent: fmtAgo(n.last_seen) }) },
    { k: 'first_seen', t: 'Primo visto', f: n => fmtDate(n.first_seen) },
    { k: 'adverts_7d', t: 'Advert 7g', num: true, f: n => fmtN(n.adverts_7d) },
    { k: 'adverts', t: 'Totale', num: true, f: n => fmtN(n.adverts) },
    { k: 'hops', t: 'Salti', num: true, f: n => n.hops == null ? '—' : n.hops === 0 ? 'diretto' : String(n.hops) },
    { k: 'snr', t: 'SNR', num: true, f: n => n.snr == null ? '—' : fmtN(n.snr, 1) + ' dB' },
    { k: 'rssi', t: 'RSSI', num: true, f: n => n.rssi == null ? '—' : fmtN(n.rssi) + ' dBm' },
  ];
  const LIMIT = 150, shown = stView.nodesAll ? rows : rows.slice(0, LIMIT);
  const box = $('st-nodes');
  if (!rows.length) {
    box.replaceChildren(el('p', { className: 'muted', textContent: !all.length ? 'Ancora nessun nodo: lo storico si riempie man mano che il gateway sente gli advert.'
      : f === 'new' ? 'Nessun nodo nuovo negli ultimi 7 giorni.' : f === 'gone' ? 'Nessun nodo sparito.' : 'Nessun nodo corrisponde alla ricerca.' }));
    return;
  }
  const table = sortTable(cols, shown, stView.nodeSort, s => { stView.nodeSort = s; renderNodes(); });
  const more = rows.length > LIMIT ? el('button', { className: 'small st-more', textContent: stView.nodesAll ? 'Mostra meno' : `Mostra tutti (${rows.length})` }) : null;
  if (more) more.onclick = () => { stView.nodesAll = !stView.nodesAll; renderNodes(); };
  box.replaceChildren(table, more || '');
}
document.querySelectorAll('#st-nfilter button').forEach(b => b.onclick = () => {
  stView.nodeFilter = b.dataset.f;
  document.querySelectorAll('#st-nfilter button').forEach(x => x.classList.toggle('on', x === b));
  if (stats) renderNodes();
});
$('st-nsearch').oninput = () => { if (stats) renderNodes(); };
$('st-ntype').onchange = () => { if (stats) renderNodes(); };

function availColor(ok, total) {
  if (!total) return ST_COLORS.none;
  const p = ok / total;
  return p >= 1 ? ST_COLORS.good : p >= 0.5 ? ST_COLORS.warn : p > 0 ? ST_COLORS.serious : ST_COLORS.crit;
}
function renderAvailability(list) {
  const box = $('st-avail');
  if (!list.length) { box.replaceChildren(el('p', { className: 'muted', textContent: 'Nessun ripetitore configurato: aggiungili in Gestione → Rete.' })); return; }
  const pct = v => v == null ? '—' : fmtN(v, v === 100 ? 0 : 1) + '%';
  const blocks = (arr, cls, label) => el('div', { className: cls }, ...arr.map(x => {
    const i = el('i'); i.style.background = availColor(x.ok, x.total);
    bindTip(i, () => [label(x), x.total ? `${x.ok} risposte su ${x.total} richieste (${fmtN(x.ok / x.total * 100, 0)}%)` : 'nessuna richiesta']);
    return i;
  }));
  const legend = el('div', { className: 'st-legend' }, ...[['good', 'sempre'], ['warn', 'qualche risposta persa'], ['serious', 'metà o più perse'], ['crit', 'mai'], ['none', 'nessuna richiesta']]
    .flatMap(([k, t]) => { const s = el('span', { className: 'sw' }); s.style.background = ST_COLORS[k]; return [s, el('span', { textContent: t + '  ' })]; }));
  box.replaceChildren(...list.map(a => el('div', { className: 'st-rep' },
    el('div', { className: 'st-rep-head' },
      el('b', {}, a.name),
      el('span', { className: 'st-state' }, dot(a.online ? 'ok' : a.polls_30d ? 'ko' : ''),
        !a.polls_30d ? 'mai interrogato' : a.online ? 'risponde' : a.offline_since ? `non risponde da ${fmtAgo(a.offline_since).replace(' fa', '')}` : 'non risponde')),
    el('div', { className: 'st-pcts' }, el('span', {}, '24 ore ', el('strong', { textContent: pct(a.pct_24h) })),
      el('span', {}, '7 giorni ', el('strong', { textContent: pct(a.pct_7d) })), el('span', {}, '30 giorni ', el('strong', { textContent: pct(a.pct_30d) }))),
    blocks(a.days, 'st-days', x => stDay(x.day)),
    el('div', { className: 'st-axis' }, el('span', { textContent: '30 giorni fa' }), el('span', { textContent: 'oggi' })),
    blocks(a.hours, 'st-hours', x => `${fmtHour(x.hour)}–${fmtHour(x.hour + 3600)}`),
    el('div', { className: 'st-axis' }, el('span', { textContent: '48 ore fa' }), el('span', { textContent: 'adesso' })))), legend);
}

function renderCompare(rows) {
  const box = $('st-compare');
  if (!rows.length) { box.replaceChildren(el('p', { className: 'muted', textContent: 'Nessun dato nelle ultime 24 ore.' })); return; }
  const n = (k, dec, unit = '') => r => r[k] == null ? '—' : fmtN(r[k], dec) + unit;
  const cols = [
    { k: 'name', t: 'Nodo', cls: 'name', ascFirst: true, f: r => el('span', {}, r.name, r.kind === 'gateway' ? el('span', { className: 'st-tag', textContent: 'gateway' }) : null) },
    { k: 'online_pct', t: 'Online', num: true, worse: 'low', f: n('online_pct', 0, '%'), title: 'Risposte alle richieste di stato' },
    { k: 'volts', t: 'Batteria', num: true, worse: 'low', f: n('volts', 2, ' V') },
    { k: 'noise', t: 'Rumore', num: true, worse: 'high', f: n('noise', 0, ' dBm'), title: 'Rumore di fondo: più basso è meglio' },
    { k: 'snr', t: 'SNR', num: true, worse: 'low', f: n('snr', 1, ' dB'), title: 'SNR dell\'ultimo pacchetto ricevuto' },
    { k: 'rssi', t: 'RSSI', num: true, worse: 'low', f: n('rssi', 0, ' dBm') },
    { k: 'rx_h', t: 'Ricevuti/h', num: true, f: n('rx_h', 0) },
    { k: 'tx_h', t: 'Inviati/h', num: true, f: n('tx_h', 0) },
    { k: 'flood_h', t: 'Flood/h', num: true, f: n('flood_h', 0) },
    { k: 'dups_pct', t: 'Duplicati', num: true, worse: 'high', f: n('dups_pct', 1, '%'), title: 'Duplicati scartati sul totale ricevuto' },
    { k: 'errors_h', t: 'Errori/h', num: true, worse: 'high', f: n('errors_h', 1) },
    { k: 'air_tx_pct', t: 'Airtime TX', num: true, worse: 'high', f: n('air_tx_pct', 2, '%'), title: 'Tempo in trasmissione' },
    { k: 'air_rx_pct', t: 'Airtime RX', num: true, f: n('air_rx_pct', 2, '%') },
    { k: 'queue_max', t: 'Coda max', num: true, worse: 'high', f: n('queue_max', 0) },
    { k: 'uptime', t: 'Acceso da', num: true, f: r => fmtUp(r.uptime) },
  ].filter(c => c.k === 'name' || rows.some(r => r[c.k] != null));
  box.replaceChildren(sortTable(cols, rows, stView.cmpSort, s => { stView.cmpSort = s; renderCompare(stats.compare); }));
}

function renderBattery(list) {
  const box = $('st-batt');
  if (!list.length) { box.replaceChildren(el('p', { className: 'muted', textContent: 'Nessun ripetitore configurato.' })); return; }
  box.replaceChildren(...list.map(b => {
    const lines = [];
    const thr = fmtN(b.threshold_v, 2) + ' V';
    if (b.slope_mv_day == null) lines.push('Pochi dati: servono almeno 12 ore di letture della batteria.');
    else if (b.trend === 'giù') {
      const when = b.days_left === 0 ? 'già sotto la soglia' : b.days_left == null ? '' : b.days_left > 365 ? `alla soglia di ${thr} tra oltre un anno`
        : `alla soglia di ${thr} tra circa ${fmtN(b.days_left, b.days_left < 3 ? 1 : 0)} giorni`;
      lines.push(el('span', {}, 'Scende di ', el('strong', { textContent: fmtN(-b.slope_mv_day, 0) + ' mV al giorno' }), when ? ' · ' + when : ''));
    } else if (b.trend === 'su') lines.push(el('span', {}, 'Sale di ', el('strong', { textContent: fmtN(b.slope_mv_day, 0) + ' mV al giorno' }), ' (in carica)'));
    else lines.push(el('span', {}, el('strong', { textContent: 'Stabile' }), ' (meno di 3 mV al giorno)'));
    if (b.solar) lines.push(`☀️ Probabilmente con pannello solare: sale e scende di ${fmtN(b.swing_mv / 1000, 2)} V al giorno; `
      + (b.trend === 'giù' ? 'di giorno non recupera tutto quello che consuma di notte.' : 'il bilancio giornaliero è in pari o positivo.'));
    else if (b.days_left === 0 && b.trend !== 'giù') lines.push(`Sotto la soglia di ${thr}.`);
    return el('div', { className: 'st-rep' },
      el('div', { className: 'st-rep-head' }, el('b', {}, b.name), el('span', { className: 'st-pcts' }, el('strong', { textContent: b.volts == null ? '—' : fmtN(b.volts, 2) + ' V' }))),
      ...lines.map(l => el('p', { className: 'st-batt-line' }, l)),
      chartBox(lineChart(b.points.map(([t, mv]) => [t, mv / 1000]), { color: '#0aac23', fmt: v => fmtN(v, 2), thr: b.threshold_v, thrLabel: 'soglia ' + thr, label: 'Batteria ' })));
  }));
}

function renderLinks(list) {
  const box = $('st-links');
  if (!list.length) {
    box.replaceChildren(el('p', { className: 'muted', textContent: 'Ancora nessun dato: i vicini dei ripetitori si leggono ogni 6 ore (o da Gestione → Rete → Aggiorna mappa della rete) e il segnale dei nodi sentiti direttamente arriva con i loro advert.' }));
    return;
  }
  const LIMIT = 10, shown = stView.linksAll ? list : list.slice(0, LIMIT);
  const rows = shown.map(l => {
    const d = l.delta;
    const delta = d == null ? el('span', { textContent: 'nessun confronto (serve una settimana di dati)' })
      : el('span', { className: 'st-delta ' + (d <= -1 ? 'down' : d >= 1 ? 'up' : ''),
        textContent: (d <= -1 ? '▼ ' : d >= 1 ? '▲ ' : '= ') + fmtN(Math.abs(d), 1) + ' dB rispetto alla settimana prima' });
    const sp = spark(l.points, '#338ced', 0.5, `SNR ${fmtN(l.min, 1)} – ${fmtN(l.max, 1)} dB in 30 giorni`);
    const row = el('div', { className: 'st-link' },
      el('div', { className: 't', title: `${l.from} sente ${l.to}` }, `${l.from} → ${l.to}`, l.kind === 'gateway' ? el('span', { className: 'st-tag', textContent: 'diretto' }) : null),
      el('div', { className: 'm' }, `ora ${fmtN(l.last, 1)} dB · media 7g ${l.avg_7d == null ? '—' : fmtN(l.avg_7d, 1) + ' dB'}`, el('br'), delta),
      sp);
    bindTip(row, () => [`${l.from} sente ${l.to}`, `ultimo SNR ${fmtN(l.last, 1)} dB (${fmtAgo(l.last_ts)})`, `min ${fmtN(l.min, 1)} · max ${fmtN(l.max, 1)} dB in 30 giorni`, `${l.samples} letture`]);
    return row;
  });
  const more = list.length > LIMIT ? el('button', { className: 'small st-more', textContent: stView.linksAll ? 'Mostra meno' : `Mostra tutti (${list.length})` }) : null;
  if (more) more.onclick = () => { stView.linksAll = !stView.linksAll; renderLinks(stats.links); };
  box.replaceChildren(...rows, more || '');
}

function renderStats() {
  if (!stats) return;
  renderKpi(stats.summary);
  renderTrend(stats.trend);
  renderNodes();
  renderAvailability(stats.availability);
  renderCompare(stats.compare);
  renderBattery(stats.battery);
  renderLinks(stats.links);
  requestAnimationFrame(drawCharts);
}

async function loadStats() {
  if (statsBusy) return;
  statsBusy = true; $('st-reload').disabled = true;
  try {
    stats = await api('/api/stats?days=' + $('st-days').value);
    statsAt = Date.now();
    renderStats();
  } catch (e) { if (e.message !== 'login') toast('Statistiche: ' + e.message, true); }
  finally { statsBusy = false; $('st-reload').disabled = false; }
}
// con la scheda aperta si aggiorna quando arrivano dati nuovi (al massimo ogni 30 secondi)
function statsSoon() {
  if (!document.body.classList.contains('tab-stats')) return;
  clearTimeout(statsTimer);
  statsTimer = setTimeout(loadStats, Math.max(1000, 30000 - (Date.now() - statsAt)));
}
$('st-reload').onclick = loadStats;
$('st-days').onchange = loadStats;

// ---------- Gestione → Telegram → Avvisi nodi
async function loadNodeAlerts() {
  let a; try { a = await api('/api/alerts/nodes'); } catch (e) { return; }
  const f = $('form-nodes-alert');
  f.new.checked = a.new; f.gone.value = a.gone; f.gone_h.value = a.gone_h;
  f.gone_h.disabled = a.gone === 'off';
}
$('form-nodes-alert').gone.onchange = e => { e.target.form.gone_h.disabled = e.target.value === 'off'; };
$('form-nodes-alert').onsubmit = e => {
  e.preventDefault(); const f = e.target;
  action(f.querySelector('.primary'), () => put('/api/alerts/nodes', { new: f.new.checked, gone: f.gone.value, gone_h: +f.gone_h.value || 72 }),
    'Impostazioni salvate').then(loadNodeAlerts);
};
