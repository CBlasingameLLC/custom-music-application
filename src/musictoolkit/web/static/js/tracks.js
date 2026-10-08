// Track lists: a virtualized table that stays fast with tens of thousands of songs,
// plus the actions (menu, favorite, rating) that apply to tracks anywhere in the app.
import { api, fmt, html, useCallback, useEffect, useRef, useState, useStore } from './lib.js';
import { Icon } from './icons.js';
import { Cover, Rating } from './components.js';
import { addToQueue, player, playQueue, toggle, updateTrack } from './player.js';
import { desktop, enc, go, notifyError, openMenu, toast } from './state.js';
import { openAddToPlaylist, openTrackInfo } from './dialogs.js';
import { openTagEditor } from './tagtools.js';

export const ROW_HEIGHT = 52;
const HEADER_HEIGHT = 40;
const PAGE = 100;
const BUFFER = 8;

// ------------------------------------------------------------ actions on tracks

export async function setFavorite(tracks, favorite) {
  const ids = tracks.map((t) => t.id);
  await api('/tracks/bulk', { method: 'POST', body: { ids, favorite } });
  ids.forEach((id) => updateTrack(id, { favorite }));
}

export async function setRating(tracks, rating) {
  const ids = tracks.map((t) => t.id);
  await api('/tracks/bulk', { method: 'POST', body: { ids, rating } });
  ids.forEach((id) => updateTrack(id, { rating }));
}

export function trackMenu(tracks, { playNow, onPatched, onRemove, removeLabel = 'Remove from playlist' } = {}) {
  const single = tracks.length === 1 ? tracks[0] : null;
  const allFavorite = tracks.every((t) => t.favorite);
  const label = single ? '' : ` (${tracks.length})`;
  const items = [
    { label: `Play${label}`, icon: 'play', action: () => (playNow ? playNow() : playQueue(tracks, 0)) },
    { label: 'Play next', icon: 'chevron-right', action: () => { addToQueue(tracks, { next: true }); toast(`Playing ${single ? `"${single.title}"` : fmt.plural(tracks.length, 'song')} next`); } },
    { label: 'Add to queue', icon: 'queue', action: () => { addToQueue(tracks); toast(`Added ${single ? `"${single.title}"` : fmt.plural(tracks.length, 'song')} to the queue`); } },
    { divider: true },
    { label: 'Add to playlist…', icon: 'plus', action: () => openAddToPlaylist(tracks) },
    {
      label: allFavorite ? 'Remove from favorites' : 'Add to favorites',
      icon: 'heart',
      action: () => setFavorite(tracks, !allFavorite).then(() => onPatched?.(tracks.map((t) => t.id), { favorite: !allFavorite })).catch(notifyError),
    },
    { label: `Edit tags…`, icon: 'edit', action: () => openTagEditor(tracks.map((t) => t.id)) },
  ];
  if (single) {
    items.push(
      { divider: true },
      { label: 'Go to artist', icon: 'user', action: () => go(`/artist/${enc(single.album_artist || single.artist)}`) },
      { label: 'Go to album', icon: 'disc', action: () => go(`/album/${single.album_key}`) },
      { label: 'Song details', icon: 'info', action: () => openTrackInfo(single) },
    );
    if (desktop?.showItemInFolder) {
      items.push({
        label: 'Show in folder',
        icon: 'folder',
        action: () => api(`/tracks/${single.id}`).then((t) => desktop.showItemInFolder(t.path)).catch(notifyError),
      });
    }
  }
  if (onRemove) items.push({ divider: true }, { label: removeLabel, icon: 'trash', danger: true, action: onRemove });
  return items;
}

// ------------------------------------------------------------ the table

const COLUMNS = {
  num: { label: '#', width: '52px', sort: 'track' },
  title: { label: 'Title', width: 'minmax(200px, 3fr)', sort: 'title' },
  artist: { label: 'Artist', width: 'minmax(120px, 2fr)', sort: 'artist' },
  album: { label: 'Album', width: 'minmax(120px, 2fr)', sort: 'album' },
  genre: { label: 'Genre', width: 'minmax(80px, 1fr)', sort: 'genre' },
  year: { label: 'Year', width: '60px', sort: 'year' },
  plays: { label: 'Plays', width: '60px', sort: 'plays' },
  added: { label: 'Added', width: '104px', sort: 'added' },
  rating: { label: 'Rating', width: '104px', sort: 'rating' },
  time: { label: 'Time', width: '64px', sort: 'duration' },
  heart: { label: '', width: '38px' },
  more: { label: '', width: '38px' },
};

// Which columns survive as the window narrows (least important first to go).
const DROP_ORDER = [['genre', 'added'], ['plays', 'year'], ['album', 'rating']];
function visibleColumns(wanted, width) {
  let hide = [];
  if (width < 1180) hide = hide.concat(DROP_ORDER[0]);
  if (width < 960) hide = hide.concat(DROP_ORDER[1]);
  if (width < 720) hide = hide.concat(DROP_ORDER[2]);
  return wanted.filter((c) => !hide.includes(c));
}

export function staticFetch(items) {
  return async (offset, limit) => ({ total: items.length, items: items.slice(offset, offset + limit) });
}

export function TrackTable({
  fetchPage,
  resetKey,
  columns = ['num', 'title', 'artist', 'album', 'year', 'time', 'rating', 'heart', 'more'],
  sort,
  onSort,
  onPlay,
  onTotal,
  reorderable = false,
  onReorder,
  onRemove,
  removeLabel,
  empty,
  numbering = 'index',
  thumbs = true,
}) {
  const root = useRef();
  const body = useRef();
  const cache = useRef({ pages: new Map(), inflight: new Set(), gen: 0 });
  const [, repaint] = useState(0);
  const [total, setTotal] = useState(null);
  const [range, setRange] = useState({ first: 0, last: 24 });
  const [width, setWidth] = useState(1200);
  const [selected, setSelected] = useState(() => new Set());
  const anchor = useRef(null);
  const [dropAt, setDropAt] = useState(null);
  const dragFrom = useRef(null);
  const currentId = useStore(player, (s) => s.items[s.order[s.pos]]?.id);
  const playing = useStore(player, (s) => s.playing);
  const cols = visibleColumns(columns, width);

  const itemAt = (index) => cache.current.pages.get(Math.floor(index / PAGE))?.[index % PAGE];

  const ensurePage = useCallback(
    (page) => {
      const c = cache.current;
      if (c.pages.has(page) || c.inflight.has(page)) return;
      c.inflight.add(page);
      const generation = c.gen;
      fetchPage(page * PAGE, PAGE).then(
        (result) => {
          if (generation !== c.gen) return;
          c.inflight.delete(page);
          c.pages.set(page, result.items);
          setTotal(result.total);
          onTotal?.(result.total);
          repaint((n) => n + 1);
        },
        (error) => {
          if (generation !== c.gen) return;
          c.inflight.delete(page);
          notifyError(error);
        },
      );
    },
    [fetchPage, onTotal],
  );

  // New query: forget everything and start from the top.
  useEffect(() => {
    cache.current = { pages: new Map(), inflight: new Set(), gen: cache.current.gen + 1 };
    setTotal(null);
    setSelected(new Set());
    anchor.current = null;
    ensurePage(0);
  }, [resetKey]);

  // Work out which rows are on screen from the scrolling view around us.
  const measure = useCallback(() => {
    const scroller = root.current?.closest('.view');
    if (!scroller || !body.current) return;
    const above = Math.max(0, scroller.getBoundingClientRect().top - body.current.getBoundingClientRect().top);
    const first = Math.max(0, Math.floor(above / ROW_HEIGHT) - BUFFER);
    const last = Math.ceil((above + scroller.clientHeight) / ROW_HEIGHT) + BUFFER;
    setRange((prev) => (prev.first === first && prev.last === last ? prev : { first, last }));
    setWidth(root.current.clientWidth);
  }, []);

  useEffect(() => {
    const scroller = root.current?.closest('.view');
    if (!scroller) return undefined;
    measure();
    scroller.addEventListener('scroll', measure, { passive: true });
    const observer = new ResizeObserver(measure);
    observer.observe(scroller);
    observer.observe(root.current);
    return () => {
      scroller.removeEventListener('scroll', measure);
      observer.disconnect();
    };
  }, [measure]);

  useEffect(measure, [total]);

  useEffect(() => {
    if (total === null) return;
    const last = Math.min(range.last, total - 1);
    for (let page = Math.floor(range.first / PAGE); page <= Math.floor(last / PAGE); page++) ensurePage(page);
  }, [range.first, range.last, total, ensurePage]);

  const patchLoaded = (ids, patch) => {
    const wanted = new Set(ids);
    for (const items of cache.current.pages.values()) items.forEach((t, i) => wanted.has(t.id) && (items[i] = { ...t, ...patch }));
    repaint((n) => n + 1);
  };

  const loadedSelection = () => {
    const out = [];
    for (const items of cache.current.pages.values()) items.forEach((t) => selected.has(t.id) && out.push(t));
    return out;
  };

  function select(event, index, track) {
    if (event.shiftKey && anchor.current !== null) {
      const [from, to] = [Math.min(anchor.current, index), Math.max(anchor.current, index)];
      const next = new Set(event.ctrlKey || event.metaKey ? selected : []);
      for (let i = from; i <= to; i++) {
        const t = itemAt(i);
        if (t) next.add(t.id);
      }
      setSelected(next);
      return;
    }
    anchor.current = index;
    if (event.ctrlKey || event.metaKey) {
      const next = new Set(selected);
      next.has(track.id) ? next.delete(track.id) : next.add(track.id);
      setSelected(next);
    } else {
      setSelected(new Set([track.id]));
    }
  }

  function rowMenu(event, index, track) {
    const tracks = selected.has(track.id) && selected.size > 1 ? loadedSelection() : [track];
    if (!selected.has(track.id)) setSelected(new Set([track.id]));
    openMenu(
      event,
      trackMenu(tracks, {
        playNow: tracks.length === 1 ? () => onPlay?.(index) : undefined,
        onPatched: patchLoaded,
        onRemove: onRemove ? () => onRemove(tracks) : undefined,
        removeLabel,
      }),
    );
  }

  function keyDown(event) {
    if (event.target.closest('input, textarea, select')) return;
    const picked = [...selected];
    if (event.key === 'Enter' && picked.length === 1) {
      for (let i = 0; i < (total ?? 0); i++) if (itemAt(i)?.id === picked[0]) return onPlay?.(i);
    } else if ((event.key === 'a' || event.key === 'A') && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      const all = new Set();
      for (const items of cache.current.pages.values()) items.forEach((t) => all.add(t.id));
      setSelected(all);
    } else if (event.key === 'Delete' && onRemove && picked.length) {
      onRemove(loadedSelection());
    } else if (event.key === 'Escape') {
      setSelected(new Set());
    }
  }

  const gridTemplate = cols.map((c) => COLUMNS[c].width).join(' ');
  const selectedTracks = selected.size ? loadedSelection() : [];

  function renderRow(index) {
    const track = itemAt(index);
    const style = { top: index * ROW_HEIGHT + 'px', gridTemplateColumns: gridTemplate };
    if (!track) {
      return html`<div class="trow skeleton" key=${'s' + index} style=${style}>${cols.map((c) => html`<div class="tcell ${c}"><span class="bar"></span></div>`)}</div>`;
    }
    const isCurrent = track.id === currentId;
    const isSelected = selected.has(track.id);
    const cell = {
      num: html`<div class="tcell num">
        <span class="num-text">${isCurrent && playing ? html`<span class="eq-bars"><i></i><i></i><i></i></span>` : numbering === 'track' ? track.track || '' : index + 1}</span>
        <button class="row-play" aria-label="Play ${track.title}" onClick=${(e) => { e.stopPropagation(); isCurrent ? toggle() : onPlay?.(index); }}>
          <${Icon} name=${isCurrent && playing ? 'pause' : 'play'} size=${16} />
        </button>
      </div>`,
      title: html`<div class="tcell title">
        ${thumbs && html`<${Cover} trackId=${track.id} name=${track.album} size=40 />`}
        <div class="title-text">
          <span class="t1">${track.title}</span>
          ${!cols.includes('artist') && html`<span class="t2">${track.artist}</span>`}
        </div>
      </div>`,
      artist: html`<div class="tcell artist"><a href="#/artist/${enc(track.album_artist || track.artist)}" onClick=${(e) => e.stopPropagation()}>${track.artist}</a></div>`,
      album: html`<div class="tcell album"><a href="#/album/${track.album_key}" onClick=${(e) => e.stopPropagation()}>${track.album}</a></div>`,
      genre: html`<div class="tcell dim">${track.genre || ''}</div>`,
      year: html`<div class="tcell dim">${track.year || ''}</div>`,
      plays: html`<div class="tcell dim">${track.plays || ''}</div>`,
      added: html`<div class="tcell dim">${fmt.date(track.added)}</div>`,
      rating: html`<div class="tcell"><${Rating} value=${track.rating} onChange=${(r) => setRating([track], r).then(() => patchLoaded([track.id], { rating: r })).catch(notifyError)} /></div>`,
      time: html`<div class="tcell dim time">${fmt.time(track.duration)}</div>`,
      heart: html`<div class="tcell">
        <button class="icon-btn heart ${track.favorite ? 'on' : ''}" aria-label=${track.favorite ? 'Remove from favorites' : 'Add to favorites'} aria-pressed=${track.favorite}
          onClick=${(e) => { e.stopPropagation(); setFavorite([track], !track.favorite).then(() => patchLoaded([track.id], { favorite: !track.favorite })).catch(notifyError); }}>
          <${Icon} name="heart" size=${16} fill=${track.favorite} />
        </button></div>`,
      more: html`<div class="tcell"><button class="icon-btn" aria-label="More actions" onClick=${(e) => {
        e.stopPropagation();
        const box = e.currentTarget.getBoundingClientRect();
        rowMenu({ clientX: box.left, clientY: box.bottom + 4 }, index, track);
      }}><${Icon} name="more" size=${16} /></button></div>`,
    };
    return html`<div
      class="trow ${isCurrent ? 'current' : ''} ${isSelected ? 'selected' : ''} ${dropAt === index ? 'drop-before' : ''}"
      key=${track.id + ':' + index}
      style=${style}
      draggable=${reorderable}
      onClick=${(e) => select(e, index, track)}
      onDblClick=${() => onPlay?.(index)}
      onContextMenu=${(e) => rowMenu(e, index, track)}
      onDragStart=${(e) => { dragFrom.current = index; e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', String(index)); }}
      onDragOver=${(e) => { if (reorderable && dragFrom.current !== null) { e.preventDefault(); setDropAt(index); } }}
      onDragEnd=${() => { dragFrom.current = null; setDropAt(null); }}
      onDrop=${(e) => { e.preventDefault(); const from = dragFrom.current; dragFrom.current = null; setDropAt(null); if (from !== null && from !== index) onReorder?.(from, index); }}
    >${cols.map((c) => cell[c])}</div>`;
  }

  const rows = [];
  if (total) for (let i = range.first; i <= Math.min(range.last, total - 1); i++) rows.push(renderRow(i));

  return html`<div class="ttable" ref=${root} tabIndex="0" onKeyDown=${keyDown}>
    ${selected.size > 1
      ? html`<div class="thead selection-bar" style=${{ height: HEADER_HEIGHT + 'px' }}>
          <strong>${fmt.plural(selected.size, 'song')} selected</strong>
          <button class="chip-btn" onClick=${() => playQueue(selectedTracks, 0)}><${Icon} name="play" size=${14} /> Play</button>
          <button class="chip-btn" onClick=${() => { addToQueue(selectedTracks); toast(`Added ${fmt.plural(selectedTracks.length, 'song')} to the queue`); }}><${Icon} name="queue" size=${14} /> Queue</button>
          <button class="chip-btn" onClick=${() => openAddToPlaylist(selectedTracks)}><${Icon} name="plus" size=${14} /> Playlist</button>
          <button class="chip-btn" onClick=${() => openTagEditor(selectedTracks.map((t) => t.id))}><${Icon} name="edit" size=${14} /> Edit tags</button>
          <button class="chip-btn" onClick=${() => setFavorite(selectedTracks, true).then(() => patchLoaded(selectedTracks.map((t) => t.id), { favorite: true })).catch(notifyError)}><${Icon} name="heart" size=${14} /> Favorite</button>
          ${onRemove && html`<button class="chip-btn danger" onClick=${() => onRemove(selectedTracks)}><${Icon} name="trash" size=${14} /> Remove</button>`}
          <span class="spacer"></span>
          <button class="chip-btn" onClick=${() => setSelected(new Set())}>Clear</button>
        </div>`
      : html`<div class="thead" style=${{ gridTemplateColumns: gridTemplate, height: HEADER_HEIGHT + 'px' }}>
          ${cols.map((c) => {
            const col = COLUMNS[c];
            const active = sort && col.sort && sort.key === col.sort;
            return html`<div class="hcell ${c} ${col.sort && onSort ? 'sortable' : ''} ${active ? 'active' : ''}" onClick=${() => col.sort && onSort?.(col.sort)}>
              ${c === 'time' ? html`<${Icon} name="clock" size=${14} title="Length" />` : col.label}
              ${active && html`<${Icon} name=${sort.dir === 'asc' ? 'chevron-up' : 'chevron-down'} size=${13} />`}
            </div>`;
          })}
        </div>`}
    ${total === 0
      ? empty
      : html`<div class="tbody" ref=${body} style=${{ height: (total ?? 8) * ROW_HEIGHT + 'px' }}>${total === null ? Array.from({ length: 8 }, (_, i) => renderRow(i)) : rows}</div>`}
  </div>`;
}
