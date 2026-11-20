// MeshWatch — mappa: mappe di base, tracce e livello della rete
// ---------- mappa
const map = L.map('map').setView([42.5, 12.5], 6);
// mappe di base, tutte gratuite e senza chiave; la scelta resta salvata nel browser
const OSM = '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';
const CARTO = OSM + ' © <a href="https://carto.com/attributions">CARTO</a>';
const esriImg = () => L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', { maxZoom: 19, attribution: '© Esri, Maxar, Earthstar Geographics' });
const BASES = {
  'Chiara (CARTO Voyager)': L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', { maxZoom: 20, subdomains: 'abcd', attribution: CARTO }),
  'Scura (CARTO Dark Matter)': L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', { maxZoom: 20, subdomains: 'abcd', attribution: CARTO }),
  'OpenStreetMap': L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: OSM, referrerPolicy: 'origin' }),
  'Escursionismo (CyclOSM)': L.tileLayer('https://{s}.tile-cyclosm.openstreetmap.fr/cyclosm/{z}/{x}/{y}.png', { maxZoom: 20, attribution: OSM + ' · CyclOSM', referrerPolicy: 'origin' }),
  'Topografica (OpenTopoMap)': L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', { maxZoom: 17, attribution: OSM + ' · © OpenTopoMap (CC-BY-SA)' }),
  'Satellite (Esri)': esriImg(),
  'Satellite con nomi': L.layerGroup([esriImg(), L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', { maxZoom: 19 })]),
};
let baseName = 'Chiara (CARTO Voyager)';
try { if (BASES[localStorage.getItem('baseLayer')]) baseName = localStorage.getItem('baseLayer'); } catch (e) { }
BASES[baseName].addTo(map);
const trackLayer = L.layerGroup().addTo(map);
const netLayer = L.layerGroup();   // mappa della rete MeshCore (solo con login)
const layersCtl = L.control.layers(BASES, {}).addTo(map);
map.on('baselayerchange', e => { try { localStorage.setItem('baseLayer', e.name); } catch (err) { } });
map.on('overlayadd', e => { if (e.layer === netLayer) { try { localStorage.setItem('netLayer', '1'); } catch (err) { } loadNetwork(); netLegend.addTo(map); } });
map.on('overlayremove', e => { if (e.layer === netLayer) { try { localStorage.setItem('netLayer', '0'); } catch (err) { } netLegend.remove(); } });
const netLegend = L.control({ position: 'bottomleft' });
netLegend.onAdd = () => {
  const d = L.DomUtil.create('div', 'net-legend');
  d.innerHTML = '<b>Rete MeshCore</b><br><i style="background:#3ccf8e"></i>SNR ≥ 5 dB<br><i style="background:#e8c547"></i>SNR 0–5 dB<br>'
    + '<i style="background:#ff7a45"></i>SNR &lt; 0 dB<br><i style="background:#8a918f;height:0;border-top:2px dashed #8a918f"></i>sentito dal gateway';
  return d;
};
// pulsante "pulisci tracce" sulla mappa (solo admin)
L.Control.Clear = L.Control.extend({
  options: { position: 'topleft' },
  onAdd() {
    const box = L.DomUtil.create('div', 'leaflet-bar admin-only clear-ctl');
    const a = L.DomUtil.create('a', '', box);
    a.href = '#'; a.title = 'Pulisci tracce'; a.setAttribute('role', 'button'); a.setAttribute('aria-label', 'Pulisci tracce');
    a.innerHTML = '<svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6"/></svg>';
    L.DomEvent.disableClickPropagation(box);
    L.DomEvent.on(a, 'click', e => { L.DomEvent.preventDefault(e); clearTracks(null); });
    return box;
  }
});
new L.Control.Clear().addTo(map);

// ---------- mappa della rete: nodi con posizione e collegamenti tra vicini
const snrColor = v => v == null ? '#8a918f' : v >= 5 ? '#3ccf8e' : v >= 0 ? '#e8c547' : '#ff7a45';
const NODE_CLS = { 'ripetitore': 'rep', 'companion': 'cmp', 'room server': 'room' };
const hopsText = h => h == null || h < 0 ? 'percorso sconosciuto (flood)' : h === 0 ? 'sentito direttamente' : `${h} salt${h === 1 ? 'o' : 'i'}`;
async function loadNetwork() {
  if (!me || me.role === 'guest' || !map.hasLayer(netLayer)) return;
  let n; try { n = await api('/api/network'); } catch (e) { return; }
  netLayer.clearLayers();
  n.links.forEach(l => {
    const direct = l.kind === 'direct';
    L.polyline([l.from, l.to], { color: direct ? '#8a918f' : snrColor(l.snr), weight: direct ? 2 : 3, opacity: .85, dashArray: direct ? '5 6' : null })
      .bindPopup(() => el('div', {}, el('strong', { textContent: `${l.a} ↔ ${l.b}` }), el('br'),
        direct ? 'sentito direttamente dal gateway' : `SNR ${l.snr != null ? (+l.snr).toFixed(1) + ' dB' : '—'} · sentito ${l.secs_ago != null ? fmtAgo(Date.now() / 1000 - l.secs_ago) : '—'}`))
      .addTo(netLayer);
  });
  const icon = cls => L.divIcon({ className: 'node-ic ' + cls, html: '<span></span>', iconSize: [18, 18] });
  n.nodes.forEach(c => {
    L.marker([c.lat, c.lon], { icon: icon(NODE_CLS[c.type] || 'oth'), title: c.name })
      .bindPopup(() => el('div', {}, el('strong', { textContent: c.name }), el('br'), `${c.type} · ${hopsText(c.hops)}`, el('br'),
        `ultimo advert ${c.last_advert ? fmtTs(c.last_advert) : '—'}`))
      .addTo(netLayer);
  });
  if (n.gateway) L.marker([n.gateway.lat, n.gateway.lon], { icon: icon('gw'), title: n.gateway.name, zIndexOffset: 500 })
    .bindPopup(() => el('div', {}, el('strong', { textContent: n.gateway.name || 'Gateway' }), el('br'), 'gateway (radio collegata al Raspberry)'))
    .addTo(netLayer);
}
// percorso dell'ultima risposta: gateway → ripetitori → dispositivo (solo con login)
const routeLayer = L.layerGroup();
map.on('overlayadd', e => { if (e.layer === routeLayer) try { localStorage.setItem('routeLayer', '1'); } catch (err) { } });
map.on('overlayremove', e => { if (e.layer === routeLayer) try { localStorage.setItem('routeLayer', '0'); } catch (err) { } });
const routeName = n => n.name ? n.name + (n.ambiguous ? '?' : '') : n.hash;
function routeText(r) {
  if (!r) return null;
  if (r.hops < 0) return 'percorso sconosciuto (flood)';
  if (r.hops === 0 || !r.nodes.length) return 'diretto dal gateway';
  return 'via ' + r.nodes.map(routeName).join(' → ');
}
function drawRoutes(trackers) {
  routeLayer.clearLayers();
  trackers.forEach(t => {
    const r = t.route, p = t.last_position;
    if (!r || r.hops < 0 || !p) return;
    const pts = [];
    if (r.gateway && r.gateway.lat != null) pts.push([r.gateway.lat, r.gateway.lon]);
    r.nodes.forEach(n => { if (n.lat != null) pts.push([n.lat, n.lon]); });
    pts.push([p.lat, p.lon]);
    if (pts.length < 2) return;
    const missing = r.nodes.filter(n => n.lat == null).length;
    L.polyline(pts, { color: t.color, weight: 2, opacity: .9, dashArray: '2 7', lineCap: 'round' })
      .bindPopup(() => el('div', {}, el('strong', { textContent: `Percorso di ${t.label}` }), el('br'),
        [r.gateway ? r.gateway.name : 'gateway', ...r.nodes.map(routeName), t.label].join(' → '), el('br'),
        `${hopsText(r.hops)} · ${fmtTs(r.ts)}`, missing ? el('br') : null,
        missing ? `${missing} ripetitor${missing === 1 ? 'e' : 'i'} senza posizione: linea approssimata` : null))
      .addTo(routeLayer);
  });
}
const lastMarkers = {};
const isPhone = () => matchMedia('(max-width: 820px)').matches;
