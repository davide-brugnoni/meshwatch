// MeshWatch — Gestione → Dashboard: invio delle sole statistiche a una dashboard pubblica
let pubInfo = null;
function pubStateText(p) {
  const l = p.last || {};
  if (!p.enabled) return 'Spenta: non parte niente.';
  if (!p.ready) return 'Manca ' + (!p.url ? "l'indirizzo del sito" : 'il token') + ': non parte niente finché non lo imposti.';
  const last = l.ts ? (l.ok ? `ultimo invio riuscito ${fmtTs(l.ts)}${l.bytes ? ` · ${Math.round(l.bytes / 1024)} kB` : ''}`
    : `ultimo invio non riuscito ${fmtTs(l.ts)}: ${l.error || 'errore'}`) : 'nessun invio ancora (il primo parte entro pochi minuti)';
  return `Attiva: ogni ${p.interval_min} minuti verso ${p.url} · ${last}.`;
}
async function loadPublish() {
  let p; try { p = await api('/api/publish'); } catch (e) { return; }
  pubInfo = p;
  const f = $('form-pub');
  if (!f.contains(document.activeElement)) {
    f.enabled.checked = p.enabled; f.url.value = p.url || ''; f.interval_min.value = p.interval_min;
    f.title.value = p.title || ''; f.grid_m.value = String(p.grid_m); f.companions.checked = p.companions;
    f.token.value = '';
    f.token.placeholder = p.token_set ? 'impostato · lascia vuoto per non cambiarlo' : 'nessun token: incollalo o generane uno';
    $('pub-sections').replaceChildren(...Object.entries(p.all_sections).map(([k, label]) => {
      const c = el('input', { type: 'checkbox', name: 'sec', value: k, checked: p.sections.includes(k) });
      return el('label', {}, c, label);
    }));
  }
  $('pub-clear').hidden = !p.token_set;
  const st = $('pub-state');
  st.textContent = pubStateText(p);
  st.classList.toggle('warn', p.enabled && (!p.ready || (p.last && p.last.ok === false)));
}
$('form-pub').onsubmit = e => {
  e.preventDefault(); const f = e.target;
  const body = { enabled: f.enabled.checked, url: f.url.value.trim(), interval_min: +f.interval_min.value || 15,
    title: f.title.value.trim(), grid_m: +f.grid_m.value, companions: f.companions.checked,
    sections: [...f.querySelectorAll('input[name=sec]:checked')].map(x => x.value) };
  if (f.token.value.trim()) body.token = f.token.value.trim();
  if (body.url && !/^https:\/\//.test(body.url)) { toast("L'indirizzo deve iniziare con https://", true); return; }
  action(f.querySelector('.primary'), () => put('/api/publish', body), 'Impostazioni salvate').then(() => { $('pub-newtoken').hidden = true; loadPublish(); });
};
$('pub-send').onclick = e => action(e.target, () => post('/api/publish/send'), 'Statistiche inviate').then(loadPublish);
$('pub-preview').onclick = () => window.open('/api/publish/preview', '_blank', 'noopener');
$('pub-gen').onclick = () => {
  const a = new Uint8Array(24); crypto.getRandomValues(a);
  const t = Array.from(a, b => b.toString(16).padStart(2, '0')).join('');
  const f = $('form-pub'); f.token.type = 'text'; f.token.value = t;
  const n = $('pub-newtoken'); n.hidden = false;
  n.textContent = 'Nuovo token: copialo anche nel secret MESHWATCH_TOKEN del sito, poi premi Salva. Dopo il salvataggio non si vede più.';
  setTimeout(() => { f.token.select(); }, 30);
};
$('pub-clear').onclick = e => {
  if (!confirm('Togliere il token? Le statistiche smettono di partire finché non ne imposti uno nuovo.')) return;
  action(e.target, () => put('/api/publish', { clear_token: true }), 'Token tolto').then(loadPublish);
};
$('form-pub').token.onblur = e => { if (!e.target.value) e.target.type = 'password'; };
