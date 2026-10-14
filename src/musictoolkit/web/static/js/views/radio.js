// Radio: live stations played through the app. Favorites and recently played ones are kept here; Browse looks in the Radio Browser
// directory (radio-browser.info), and a station can also be added by typing its address.
import { api, html, useAsync, useCallback, useDebounced, useEffect, useState, useStore } from '../lib.js';
import { Icon } from '../icons.js';
import { Button, Cover, Empty, PageHeader, Spinner, TabPanel, Tabs } from '../components.js';
import { confirmDialog, promptText } from '../dialogs.js';
import { isStation } from '../items.js';
import { current, player } from '../player.js';
import { asStation, playStation, setStationFavorite, stations } from '../radio.js';
import { notifyError, openMenu, route, toast } from '../state.js';

const TABS = [
  { id: 'favorites', label: 'Favorites', icon: 'heart' },
  { id: 'browse', label: 'Browse', icon: 'compass' },
  { id: 'recent', label: 'Recent', icon: 'clock' },
  { id: 'add', label: 'Add by address', icon: 'plus' },
];

const ORDERS = [['popular', 'Most listened'], ['votes', 'Most voted'], ['name', 'Name'], ['bitrate', 'Best sound']];

// ------------------------------------------------------------ one station

/** A station as a card: press its picture to listen, the heart to keep it among the favorites. `onChanged(kept, removed)` says what happened. */
function StationCard({ station, onChanged }) {
  const name = station.name;
  const state = useStore(player, (s) => {
    const now = current(s);
    return now && isStation(now) && now.stream_url === station.stream_url ? (s.playing ? 'playing' : 'paused') : '';
  });
  const where = [station.country, station.codec && `${station.codec}${station.bitrate ? ` ${station.bitrate} kbps` : ''}`].filter(Boolean).join(' · ');
  const tags = (station.tags || []).slice(0, 3).join(', ');

  async function toggleFavorite() {
    try {
      onChanged(await setStationFavorite(station, !station.favorite));
    } catch (error) {
      notifyError(error);
    }
  }

  async function rename() {
    const next = await promptText({ title: 'Rename station', label: 'Name', value: station.name, confirm: 'Save' });
    if (!next || next === station.name) return;
    try {
      onChanged(await api(`/radio/stations/${station.id}`, { method: 'PATCH', body: { name: next } }));
    } catch (error) {
      notifyError(error);
    }
  }

  async function remove() {
    if (!(await confirmDialog({ title: 'Remove station', message: `Remove "${station.name}" from your stations?`, confirm: 'Remove', danger: true }))) return;
    try {
      await api(`/radio/stations/${station.id}`, { method: 'DELETE' });
      onChanged(null, station);
    } catch (error) {
      notifyError(error);
    }
  }

  function openActions(event) {
    event.stopPropagation(); // the click that opens a menu must not reach the window, which closes menus on a click
    const box = event.currentTarget.getBoundingClientRect();
    openMenu({ clientX: box.left, clientY: box.bottom }, [
      { label: 'Rename…', icon: 'edit', action: rename },
      { label: 'Remove from my stations', icon: 'trash', danger: true, action: remove },
    ]);
  }

  return html`<div class="card station-card ${state ? 'current' : ''}">
    <div class="card-art">
      <${Cover} item=${asStation(station)} name=${name} size=200 />
      <button class="card-play" aria-label=${`Play ${name}`} onClick=${() => playStation(station)}><${Icon} name="play" size=${22} /></button>
      ${state === 'playing' && html`<span class="live-flag" aria-hidden="true"><span class="eq-bars"><i></i><i></i><i></i></span></span>`}
    </div>
    <div class="card-title" title=${name}>${name}</div>
    <div class="card-sub">${where || 'Live radio'}</div>
    ${tags ? html`<div class="card-sub">${tags}</div>` : null}
    <div class="card-foot">
      <button class="icon-btn heart ${station.favorite ? 'on' : ''}" aria-pressed=${Boolean(station.favorite)} aria-label=${`Favorite: ${name}`} onClick=${toggleFavorite}>
        <${Icon} name="heart" size=${18} fill=${Boolean(station.favorite)} />
      </button>
      ${station.id && html`<button class="icon-btn" aria-label=${`More about ${name}`} aria-haspopup="menu" onClick=${openActions}><${Icon} name="more" size=${18} /></button>`}
    </div>
  </div>`;
}

// ------------------------------------------------------------ the stations kept here

function KeptStations({ scope, version, onChanged, onCounts, go }) {
  const { data, error } = useAsync(() => api('/radio/stations', { params: { scope } }), [scope, version]);
  useEffect(() => data && onCounts(data.counts), [data]);
  if (error) return html`<${Empty} icon="alert" title="The stations could not be read">${error.message}<//>`;
  if (!data) return html`<${Spinner} />`;
  if (!data.items.length) {
    return scope === 'favorites'
      ? html`<${Empty} icon="radio" title="No favorite stations yet">
          <p>Find stations to listen to in Browse, or add one by the address of its stream. The heart on a station keeps it here.</p>
          <div class="row-actions"><${Button} kind="primary" icon="compass" onClick=${() => go('browse')}>Browse stations<//><${Button} icon="plus" onClick=${() => go('add')}>Add by address<//></div>
        <//>`
      : html`<${Empty} icon="clock" title="Nothing played yet"><p>The stations you listen to show up here, the latest first.</p><//>`;
  }
  return html`<div class="grid">${data.items.map((s) => html`<${StationCard} key=${s.id} station=${s} onChanged=${onChanged} />`)}</div>`;
}

// ------------------------------------------------------------ the directory

function BrowseStations({ onChanged }) {
  const [text, setText] = useState('');
  const [tag, setTag] = useState('');
  const [country, setCountry] = useState('');
  const [order, setOrder] = useState('popular');
  const [attempt, setAttempt] = useState(0);
  const search = useDebounced(text.trim(), 350);
  const [found, setFound] = useState({ items: [], more: false, loading: true, error: null });
  const { data: tags } = useAsync(() => api('/radio/directory/tags').catch(() => ({ items: [] })), [attempt]);
  const { data: countries } = useAsync(() => api('/radio/directory/countries').catch(() => ({ items: [] })), [attempt]);

  useEffect(() => {
    let stale = false;
    setFound((f) => ({ ...f, loading: true, error: null }));
    api('/radio/directory', { params: { q: search, tag, country, order, offset: 0 } }).then(
      (answer) => !stale && setFound({ items: answer.items, more: answer.more, loading: false, error: null }),
      (error) => !stale && setFound({ items: [], more: false, loading: false, error }),
    );
    return () => { stale = true; };
  }, [search, tag, country, order, attempt]);

  async function showMore() {
    try {
      const answer = await api('/radio/directory', { params: { q: search, tag, country, order, offset: found.items.length } });
      setFound((f) => ({ ...f, items: [...f.items, ...answer.items.filter((s) => !f.items.some((k) => k.stream_url === s.stream_url))], more: answer.more }));
    } catch (error) {
      notifyError(error);
    }
  }

  const changed = (kept) => {
    if (kept) setFound((f) => ({ ...f, items: f.items.map((s) => (s.stream_url === kept.stream_url ? { ...s, ...kept } : s)) }));
    onChanged();
  };

  return html`<div class="browse">
    <div class="browse-bar">
      <label class="search-box"><${Icon} name="search" size=${16} />
        <input type="search" value=${text} placeholder="Search stations by name" aria-label="Search stations by name" onInput=${(e) => setText(e.target.value)} /></label>
      <label class="sort-select"><span>Country</span>
        <select value=${country} onChange=${(e) => setCountry(e.target.value)}>
          <option value="">Anywhere</option>
          ${(countries?.items || []).map((c) => html`<option value=${c.code} key=${c.code}>${c.name}</option>`)}
        </select></label>
      <label class="sort-select"><span>Sort by</span>
        <select value=${order} onChange=${(e) => setOrder(e.target.value)}>${ORDERS.map(([id, label]) => html`<option value=${id} key=${id}>${label}</option>`)}</select></label>
    </div>
    ${(tags?.items || []).length > 0 && html`<div class="chip-row" role="group" aria-label="Kinds of music">
      ${tags.items.slice(0, 18).map((t) => html`<button type="button" class="chip ${tag === t.name ? 'on' : ''}" key=${t.name} aria-pressed=${tag === t.name}
        onClick=${() => setTag(tag === t.name ? '' : t.name)}>${t.name}</button>`)}
    </div>`}
    ${found.error
      ? html`<${Empty} icon="alert" title="The directory could not be reached"><p>${found.error.message}</p><${Button} icon="refresh" onClick=${() => setAttempt((n) => n + 1)}>Try again<//><//>`
      : found.loading && !found.items.length
        ? html`<${Spinner} label="Looking for stations…" />`
        : !found.items.length
          ? html`<${Empty} icon="radio" title="No stations found"><p>Try other words, another kind of music, or a different country.</p><//>`
          : html`<div class="grid">${found.items.map((s) => html`<${StationCard} key=${s.stream_url} station=${s} onChanged=${changed} />`)}</div>
              ${found.more && html`<div class="more-row"><${Button} onClick=${showMore}>Show more stations<//></div>`}`}
  </div>`;
}

// ------------------------------------------------------------ by address

function AddByAddress({ onAdded }) {
  const [url, setUrl] = useState('');
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState('');

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setProblem('');
    try {
      const station = await api('/radio/stations', { method: 'POST', body: { url: url.trim(), name: name.trim() || undefined } });
      toast(`Added "${station.name}" to your favorites`, 'success');
      setUrl('');
      setName('');
      onAdded(station);
    } catch (error) {
      setProblem(error.message);
    } finally {
      setBusy(false);
    }
  }

  return html`<form class="card-panel add-form" onSubmit=${submit}>
    <h2><${Icon} name="plus" size=${20} /> Add a station by its address</h2>
    <p class="subtle">Paste the address of a stream, or of the playlist file (.pls, .m3u) a station publishes. It is tried first: an address that does not play is not kept, and what the stream says about itself fills in the name.</p>
    <label class="field"><span>Stream address</span>
      <input type="url" value=${url} required placeholder="https://example.com/live.mp3" onInput=${(e) => setUrl(e.target.value)} /></label>
    <label class="field"><span>Name (optional)</span>
      <input type="text" value=${name} maxlength="200" onInput=${(e) => setName(e.target.value)} /></label>
    ${problem ? html`<p class="note warn" role="alert"><${Icon} name="alert" size=${14} /><span>${problem}</span></p>` : null}
    <div class="row-actions"><${Button} kind="primary" icon="plus" type="submit" disabled=${busy || !url.trim()}>${busy ? 'Trying the address…' : 'Add station'}<//></div>
  </form>`;
}

// ------------------------------------------------------------ the view

export function RadioView() {
  const { query } = useStore(route);
  const [tab, setTab] = useState(TABS.some((t) => t.id === query.tab) ? query.tab : 'favorites');
  useEffect(() => { if (TABS.some((t) => t.id === query.tab)) setTab(query.tab); }, [query.tab]); // a link to #/radio?tab=browse from where the Radio screen is already open
  const [version, setVersion] = useState(0);
  const [counts, setCounts] = useState({});
  // A station added, removed, favorited or renamed on this screen (or favorited from the player bar): the lists that show it read it again.
  const elsewhere = useStore(stations, (s) => s.rev);
  const changed = useCallback(() => setVersion((v) => v + 1), []);

  return html`
    <${PageHeader} title="Radio" subtitle="Live stations from around the world, played through the app. Nothing is recorded or downloaded." />
    <${Tabs} id="radio" label="Radio" value=${tab} onChange=${setTab}
      tabs=${TABS.map((t) => ({ ...t, count: t.id === 'favorites' || t.id === 'recent' ? counts[t.id] || undefined : undefined }))} />
    <${TabPanel} id="radio" value=${tab}>
      ${(tab === 'favorites' || tab === 'recent') && html`<${KeptStations} key=${tab} scope=${tab} version=${version + elsewhere} onChanged=${changed} onCounts=${setCounts} go=${setTab} />`}
      ${tab === 'browse' && html`<${BrowseStations} onChanged=${changed} />`}
      ${tab === 'add' && html`<${AddByAddress} onAdded=${() => { changed(); setTab('favorites'); }} />`}
    <//>
    <p class="hint-card subtle"><${Icon} name="info" size=${14} /><span>Stations are found in the free <a href="https://www.radio-browser.info" target="_blank" rel="noopener noreferrer">Radio Browser</a> directory, which is asked only when you look in Browse. Some stations only play to their own players or use streams (HLS) that this app cannot play.</span></p>`;
}
