// Filtering: the chip bar used by the library views, the rule editor, and smart playlists.
import { api, fmt, Fragment, html, useDebounced, useEffect, useRef, useState, useStore } from './lib.js';
import { Icon } from './icons.js';
import { Button } from './components.js';
import { go, library, loadFacets, loadPlaylists, notifyError, openModal, toast } from './state.js';

export const emptyFilters = () => ({
  genres: [], favorite: false, unplayed: false, addedDays: 0, minRating: 0, yearFrom: '', yearTo: '', formats: [], custom: [],
});

/** The chip-bar state as the {match, rules} document the API understands (or null for "no filter"). */
export function filtersToRules(f) {
  const rules = [];
  if (f.genres.length) rules.push({ field: 'genre', op: 'is_any', value: f.genres });
  if (f.formats.length) rules.push({ field: 'format', op: 'is_any', value: f.formats });
  if (f.favorite) rules.push({ field: 'favorite', op: 'is', value: true });
  if (f.unplayed) rules.push({ field: 'unplayed', op: 'is', value: true });
  if (f.addedDays) rules.push({ field: 'added_within_days', op: 'is', value: f.addedDays });
  if (f.minRating) rules.push({ field: 'rating', op: '>=', value: f.minRating });
  if (f.yearFrom !== '' || f.yearTo !== '') {
    rules.push({ field: 'year', op: 'between', value: [Number(f.yearFrom || 0), Number(f.yearTo || 9999)] });
  }
  rules.push(...f.custom);
  return rules.length ? { match: 'all', rules } : null;
}

export const activeFilterCount = (f) => (filtersToRules(f)?.rules.length ?? 0);

const FIELDS = [
  { key: 'title', label: 'Title', type: 'text' },
  { key: 'artist', label: 'Artist', type: 'text' },
  { key: 'album_artist', label: 'Album artist', type: 'text' },
  { key: 'album', label: 'Album', type: 'text' },
  { key: 'genre', label: 'Genre', type: 'text' },
  { key: 'format', label: 'File type', type: 'text' },
  { key: 'path', label: 'File path', type: 'text' },
  { key: 'year', label: 'Year', type: 'num' },
  { key: 'rating', label: 'Rating (0-5)', type: 'num' },
  { key: 'plays', label: 'Play count', type: 'num' },
  { key: 'bitrate', label: 'Bitrate (kbps)', type: 'num' },
  { key: 'duration', label: 'Length (seconds)', type: 'num' },
  { key: 'track', label: 'Track number', type: 'num' },
  { key: 'added_within_days', label: 'Added within the last (days)', type: 'days' },
  { key: 'played_within_days', label: 'Played within the last (days)', type: 'days' },
  { key: 'not_played_within_days', label: 'Not played in the last (days)', type: 'days' },
  { key: 'favorite', label: 'Is a favorite', type: 'bool' },
  { key: 'unplayed', label: 'Has never been played', type: 'bool' },
  { key: 'missing_tags', label: 'Is missing title, artist or album', type: 'bool' },
];
const FIELD = Object.fromEntries(FIELDS.map((f) => [f.key, f]));
const TEXT_OPS = [['is', 'is'], ['is_not', 'is not'], ['contains', 'contains'], ['not_contains', 'does not contain'], ['starts_with', 'starts with'], ['empty', 'is empty'], ['not_empty', 'is not empty']];
const NUM_OPS = [['=', 'is'], ['!=', 'is not'], ['>', 'is greater than'], ['>=', 'is at least'], ['<', 'is less than'], ['<=', 'is at most'], ['between', 'is between']];

export function describeRule(rule) {
  const field = FIELD[rule.field];
  if (!field) return rule.field;
  if (field.type === 'bool') return rule.value ? field.label : `Not: ${field.label.toLowerCase()}`;
  if (field.type === 'days') return `${field.label} ${rule.value}`;
  const ops = field.type === 'text' ? TEXT_OPS : NUM_OPS;
  const op = ops.find(([k]) => k === rule.op)?.[1] || rule.op;
  const value = Array.isArray(rule.value) ? rule.value.join(rule.op === 'between' ? ' and ' : ', ') : rule.value;
  return ['empty', 'not_empty'].includes(rule.op) ? `${field.label} ${op}` : `${field.label} ${op} ${value}`;
}

function newRule(key = 'genre') {
  const field = FIELD[key];
  if (field.type === 'bool') return { field: key, op: 'is', value: true };
  if (field.type === 'days') return { field: key, op: 'is', value: 30 };
  if (field.type === 'num') return { field: key, op: '>=', value: 0 };
  return { field: key, op: 'contains', value: '' };
}

function RuleRow({ rule, onChange, onRemove }) {
  const field = FIELD[rule.field] || FIELDS[0];
  const set = (patch) => onChange({ ...rule, ...patch });
  return html`<div class="rule-row">
    <select value=${rule.field} onChange=${(e) => onChange(newRule(e.target.value))} aria-label="Field">
      ${FIELDS.map((f) => html`<option value=${f.key}>${f.label}</option>`)}
    </select>
    ${field.type === 'text' && html`
      <select value=${rule.op} onChange=${(e) => set({ op: e.target.value })} aria-label="Condition">${TEXT_OPS.map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select>
      ${!['empty', 'not_empty'].includes(rule.op) && html`<input type="text" value=${Array.isArray(rule.value) ? rule.value.join(', ') : rule.value ?? ''} placeholder=${rule.op === 'is_any' ? 'a, b, c' : 'value'}
        onInput=${(e) => set({ value: rule.op === 'is_any' ? e.target.value.split(',').map((x) => x.trim()).filter(Boolean) : e.target.value })} />`}`}
    ${field.type === 'num' && html`
      <select value=${rule.op} onChange=${(e) => set({ op: e.target.value, value: e.target.value === 'between' ? [0, 0] : Array.isArray(rule.value) ? rule.value[0] : rule.value })} aria-label="Condition">${NUM_OPS.map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select>
      ${rule.op === 'between'
        ? html`<input class="short" type="number" value=${rule.value?.[0] ?? 0} onInput=${(e) => set({ value: [Number(e.target.value), rule.value?.[1] ?? 0] })} /><span class="subtle">and</span><input class="short" type="number" value=${rule.value?.[1] ?? 0} onInput=${(e) => set({ value: [rule.value?.[0] ?? 0, Number(e.target.value)] })} />`
        : html`<input class="short" type="number" value=${rule.value} onInput=${(e) => set({ value: Number(e.target.value) })} />`}`}
    ${field.type === 'days' && html`<input class="short" type="number" min="0" value=${rule.value} onInput=${(e) => set({ value: Number(e.target.value) })} /><span class="subtle">days</span>`}
    ${field.type === 'bool' && html`<select value=${rule.value ? 'yes' : 'no'} onChange=${(e) => set({ value: e.target.value === 'yes' })} aria-label="Value"><option value="yes">yes</option><option value="no">no</option></select>`}
    <button class="icon-btn" aria-label="Remove rule" onClick=${onRemove}><${Icon} name="x" size=${16} /></button>
  </div>`;
}

/** Edit a {match, rules} document. */
export function RuleEditor({ value, onChange }) {
  const rules = value?.rules || [];
  const match = value?.match || 'all';
  const update = (next) => onChange({ match, rules: next });
  return html`<div class="rule-editor">
    <div class="rule-match">Songs that match
      <select value=${match} onChange=${(e) => onChange({ match: e.target.value, rules })} aria-label="Match all or any">
        <option value="all">all</option><option value="any">any</option>
      </select> of these rules:</div>
    ${rules.map((rule, i) => html`<${RuleRow} key=${i} rule=${rule}
      onChange=${(r) => update(rules.map((x, j) => (j === i ? r : x)))}
      onRemove=${() => update(rules.filter((_, j) => j !== i))} />`)}
    <${Button} icon="plus" small onClick=${() => update([...rules, newRule()])}>Add a rule<//>
  </div>`;
}

function SmartPlaylistBody({ playlist, initialRules, close }) {
  const [name, setName] = useState(playlist?.name || '');
  const [rules, setRules] = useState(playlist?.rules || initialRules || { match: 'all', rules: [newRule('genre')] });
  const [count, setCount] = useState(null);
  const [error, setError] = useState('');
  const debounced = useDebounced(JSON.stringify(rules), 400);

  useEffect(() => {
    api('/tracks', { params: { rules: debounced, limit: 1 } }).then((r) => { setCount(r.total); setError(''); }, (e) => { setCount(null); setError(e.message); });
  }, [debounced]);

  async function save(event) {
    event.preventDefault();
    try {
      if (playlist) {
        await api(`/playlists/${playlist.id}`, { method: 'PATCH', body: { name: name.trim(), rules } });
        toast('Smart playlist updated', 'success');
      } else {
        const created = await api('/playlists', { method: 'POST', body: { name: name.trim(), kind: 'smart', rules } });
        toast(`Created smart playlist "${created.name}"`, 'success');
        go(`/playlist/${created.id}`);
      }
      loadPlaylists();
      close();
    } catch (e) { notifyError(e); }
  }

  return html`<form onSubmit=${save}>
    <label class="field"><span>Name</span><input type="text" value=${name} placeholder="e.g. Highly rated folk" onInput=${(e) => setName(e.target.value)} autofocus /></label>
    <${RuleEditor} value=${rules} onChange=${setRules} />
    <p class="subtle rule-count">${error ? html`<span class="err">${error}</span>` : count === null ? '' : `${fmt.plural(count, 'song')} match right now. The playlist keeps itself up to date.`}</p>
    <div class="modal-actions"><${Button} kind="primary" type="submit" disabled=${!name.trim() || !rules.rules.length || !!error}>${playlist ? 'Save' : 'Create'}<//></div>
  </form>`;
}

export function openSmartPlaylistEditor({ playlist, rules } = {}) {
  openModal({
    title: playlist ? 'Edit smart playlist' : 'New smart playlist',
    width: 720,
    render: (close) => html`<${SmartPlaylistBody} playlist=${playlist} initialRules=${rules} close=${close} />`,
  });
}

// ------------------------------------------------------------ the chip bar

function Popover({ label, count, icon, children }) {
  const [open, setOpen] = useState(false);
  const ref = useRef();
  useEffect(() => {
    if (!open) return undefined;
    const close = (e) => !ref.current?.contains(e.target) && setOpen(false);
    window.addEventListener('mousedown', close);
    return () => window.removeEventListener('mousedown', close);
  }, [open]);
  return html`<span class="popover-wrap" ref=${ref}>
    <button class="chip ${count ? 'on' : ''}" aria-expanded=${open} onClick=${() => setOpen(!open)}>
      ${icon && html`<${Icon} name=${icon} size=${14} />`}${label}${count ? html`<b>${count}</b>` : ''}<${Icon} name="chevron-down" size=${13} />
    </button>
    ${open && html`<div class="popover">${children(() => setOpen(false))}</div>`}
  </span>`;
}

export function FilterBar({ q, onQ, filters, onFilters, placeholder = 'Search', extra, rulesForSave, chips = true }) {
  const facets = useStore(library, (s) => s.facets);
  useEffect(() => { if (!facets) loadFacets().catch(() => {}); }, []);
  const set = (patch) => onFilters({ ...filters, ...patch });
  const toggleIn = (key, value) => set({ [key]: filters[key].includes(value) ? filters[key].filter((x) => x !== value) : [...filters[key], value] });
  const active = activeFilterCount(filters);

  return html`<div class="filter-bar">
    <label class="search-box"><${Icon} name="search" size=${16} />
      <input type="search" value=${q} placeholder=${placeholder} aria-label=${placeholder} onInput=${(e) => onQ(e.target.value)} />
    </label>
    ${chips && html`<${Fragment}>
    <${Popover} label="Genre" icon="tag" count=${filters.genres.length}>${() => html`
      <div class="popover-list">${(facets?.genres || []).map((g) => html`<label class="check-row"><input type="checkbox" checked=${filters.genres.includes(g.name)} onChange=${() => toggleIn('genres', g.name)} /><span>${g.name}</span><span class="subtle">${g.count}</span></label>`)}
      ${!facets?.genres?.length && html`<p class="subtle pad">No genres in the library yet.</p>`}</div>`}<//>
    <button class="chip ${filters.favorite ? 'on' : ''}" aria-pressed=${filters.favorite} onClick=${() => set({ favorite: !filters.favorite })}><${Icon} name="heart" size=${14} /> Favorites</button>
    <button class="chip ${filters.unplayed ? 'on' : ''}" aria-pressed=${filters.unplayed} onClick=${() => set({ unplayed: !filters.unplayed })}>Never played</button>
    <button class="chip ${filters.addedDays ? 'on' : ''}" aria-pressed=${!!filters.addedDays} onClick=${() => set({ addedDays: filters.addedDays ? 0 : 30 })}>New (30 days)</button>
    <${Popover} label=${filters.minRating ? `${filters.minRating}+ stars` : 'Rating'} icon="star" count=${0}>${(close) => html`
      <div class="popover-list">${[0, 1, 2, 3, 4, 5].map((n) => html`<button class="pick-row" onClick=${() => { set({ minRating: n }); close(); }}>${n ? `${n} star${n > 1 ? 's' : ''} and up` : 'Any rating'}${filters.minRating === n ? html`<${Icon} name="check" size=${14} />` : ''}</button>`)}</div>`}<//>
    <${Popover} label="Year" icon="clock" count=${filters.yearFrom !== '' || filters.yearTo !== '' ? 1 : 0}>${() => html`
      <div class="popover-form"><label>From <input type="number" value=${filters.yearFrom} placeholder=${facets?.years?.at(-1)?.year || '1960'} onInput=${(e) => set({ yearFrom: e.target.value })} /></label>
      <label>To <input type="number" value=${filters.yearTo} placeholder=${facets?.years?.[0]?.year || '2030'} onInput=${(e) => set({ yearTo: e.target.value })} /></label></div>`}<//>
    <button class="chip" onClick=${() => openModal({ title: 'More filters', width: 720, render: (close) => html`<${CustomFilters} filters=${filters} onApply=${(custom) => { onFilters({ ...filters, custom }); close(); }} />` })}>
      <${Icon} name="filter" size=${14} /> More${filters.custom.length ? html`<b>${filters.custom.length}</b>` : ''}</button>
    <//>`}
    ${active > 0 && html`<button class="chip clear" onClick=${() => onFilters(emptyFilters())}><${Icon} name="x" size=${13} /> Clear (${active})</button>`}
    ${active > 0 && rulesForSave && html`<button class="chip" onClick=${() => openSmartPlaylistEditor({ rules: rulesForSave })}><${Icon} name="sparkles" size=${14} /> Save as smart playlist</button>`}
    <span class="spacer"></span>
    ${extra}
  </div>`;
}

function CustomFilters({ filters, onApply }) {
  const [rules, setRules] = useState({ match: 'all', rules: filters.custom });
  return html`<${RuleEditor} value=${rules} onChange=${setRules} />
    <div class="modal-actions"><${Button} kind="primary" onClick=${() => onApply(rules.rules)}>Apply<//></div>`;
}
