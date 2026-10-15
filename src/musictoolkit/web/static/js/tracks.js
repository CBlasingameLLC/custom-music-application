// Track lists: a virtualized table that stays fast with tens of thousands of songs,
// plus the actions (menu, favorite, rating) that apply to tracks anywhere in the app.
import { api, fmt, html, useCallback, useEffect, useRef, useState, useStore } from './lib.js';
import { Icon } from './icons.js';
import { Cover, Rating } from './components.js';
import { addToQueue, player, playQueue, toggle, updateTrack } from './player.js';
import { addToBurnList, desktop, enc, go, notifyError, openMenu, toast, ui } from './state.js';
import { openAddToPlaylist, openTrackInfo } from './dialogs.js';
import { openTagEditor } from './tagtools.js';

export const ROW_HEIGHT = 52;
export const COMPACT_ROW_HEIGHT = 40;
/** How tall a song row is, for the density the person chose (the table lays rows out itself, so it needs the number). */
export const rowHeightFor = (density) => (density === 'compact' ? COMPACT_ROW_HEIGHT : ROW_HEIGHT);
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

/** Play songs that go with this one (same sittings, genre, era; no run of one artist), starting with it. */
export async function startRadio(track) {
  try {
    const { tracks } = await api(`/radio/track/${track.id}`);
    if (!tracks.length) return toast('There is nothing to build a radio from yet');
    await playQueue(tracks, 0, { shuffle: false, source: `Radio: ${track.title}` });
    toast(`Radio from "${track.title}": ${fmt.plural(tracks.length, 'song')} queued`, 'success', 3500);
  } catch (error) {
    notifyError(error);
  }
}

export function trackMenu(tracks, { playNow, onPatched, onRemove, removeLabel = 'Remove from playlist' } = {}) {
  const single = tracks.length === 1 ? tracks[0] : null;
  const allFavorite = tracks.every((t) => t.favorite);
  const label = single ? '' : ` (${tracks.length})`;
  const items = [
    { label: `Play${label}`, icon: 'play', action: () => (playNow ? playNow() : playQueue(tracks, 0)) },
    { label: 'Play next', icon: 'chevron-right', action: () => { addToQueue(tracks, { next: true }); toast(`Playing ${single ? `"${single.title}"` : fmt.plural(tracks.length, 'song')} next`); } },
    { label: 'Add to queue', icon: 'queue', action: () => { addToQueue(tracks); toast(`Added ${single ? `"${single.title}"` : fmt.plural(tracks.length, 'song')} to the queue`); } },
    ...(single ? [{ label: 'Start radio', icon: 'radio', action: () => startRadio(single) }] : []),
    { divider: true },
    { label: 'Add to playlist…', icon: 'plus', action: () => openAddToPlaylist(tracks) },
    { label: 'Add to burn list', icon: 'cd', action: () => addToBurnList(tracks) },
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

// `name` is what a screen reader says for a header that shows a symbol, or nothing.
const COLUMNS = {
  num: { label: '#', name: 'Number', width: '52px', sort: 'track' },
  title: { label: 'Title', width: 'minmax(200px, 3fr)', sort: 'title' },
  artist: { label: 'Artist', width: 'minmax(120px, 2fr)', sort: 'artist' },
  album: { label: 'Album', width: 'minmax(120px, 2fr)', sort: 'album' },
  genre: { label: 'Genre', width: 'minmax(80px, 1fr)', sort: 'genre' },
  year: { label: 'Year', width: '60px', sort: 'year' },
  plays: { label: 'Plays', width: '60px', sort: 'plays' },
  added: { label: 'Added', width: '104px', sort: 'added' },
  rating: { label: 'Rating', width: '104px', sort: 'rating' },
  time: { label: 'Time', name: 'Length', width: '64px', sort: 'duration' },
  heart: { label: '', name: 'Favorite', width: '38px' },
  more: { label: '', name: 'Actions', width: '38px' },
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

let tableCounter = 0;

/**
 * A table of songs for as many songs as there are, drawn only where it is seen. It is a grid for assistive technology and
 * for the keyboard: Tab goes to it once, then Up and Down, Page Up and Down, Home and End move through the rows (Shift
 * extends the selection, Ctrl moves without selecting), Enter plays, Ctrl+A selects all, Ctrl+Space toggles a row, 0-5 rate
 * the selection, the menu key opens the row's menu, Alt+Up and Down move a row in a list that can be reordered.
 */
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
  label = 'Songs',
}) {
  const root = useRef();
  const body = useRef();
  const uid = useRef(null);
  if (uid.current === null) uid.current = `tt${++tableCounter}`;
  const rowId = (index) => `${uid.current}-r${index}`;
  const cache = useRef({ pages: new Map(), inflight: new Set(), gen: 0 });
  const [, repaint] = useState(0);
  const [total, setTotal] = useState(null);
  const [range, setRange] = useState({ first: 0, last: 24 });
  const [width, setWidth] = useState(1200);
  const [selected, setSelected] = useState(() => new Set());
  const [cursor, setCursor] = useState(null); // the row the keyboard is on
  const anchor = useRef(null);
  const firstVisible = useRef(0);
  const pending = useRef(null); // a row the keyboard went to before its page arrived: it is selected when it has
  const kept = useRef({ index: null, at: 0 }); // where the cursor goes when a reorder has reloaded the list
  const [dropAt, setDropAt] = useState(null);
  const dragFrom = useRef(null);
  const currentId = useStore(player, (s) => s.items[s.order[s.pos]]?.id);
  const playing = useStore(player, (s) => s.playing);
  const rowHeight = rowHeightFor(useStore(ui, (s) => s.density));
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

  // New query: forget everything and start from the top (a row just moved with the keyboard keeps the cursor).
  useEffect(() => {
    cache.current = { pages: new Map(), inflight: new Set(), gen: cache.current.gen + 1 };
    setTotal(null);
    setSelected(new Set());
    anchor.current = null;
    const keep = Date.now() - kept.current.at < 3000 ? kept.current.index : null;
    setCursor(keep);
    pending.current = keep;
    ensurePage(0);
  }, [resetKey]);

  // Work out which rows are on screen from the scrolling view around us.
  const measure = useCallback(() => {
    const scroller = root.current?.closest('.view');
    if (!scroller || !body.current) return;
    const above = Math.max(0, scroller.getBoundingClientRect().top - body.current.getBoundingClientRect().top);
    const first = Math.max(0, Math.floor(above / rowHeight) - BUFFER);
    const last = Math.ceil((above + scroller.clientHeight) / rowHeight) + BUFFER;
    firstVisible.current = above > 0 ? Math.ceil((above + HEADER_HEIGHT) / rowHeight) : 0;
    setRange((prev) => (prev.first === first && prev.last === last ? prev : { first, last }));
    setWidth(root.current.clientWidth);
  }, [rowHeight]);

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

  // The keyboard went to a row that was not loaded yet: select it as soon as it is.
  useEffect(() => {
    const index = pending.current;
    const track = index === null ? null : itemAt(index);
    if (!track) return;
    pending.current = null;
    anchor.current = index;
    setSelected(new Set([track.id]));
  });

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

  function selectRange(from, to, base) {
    const next = new Set(base);
    for (let i = Math.min(from, to); i <= Math.max(from, to); i++) {
      const t = itemAt(i);
      if (t) next.add(t.id);
    }
    setSelected(next);
  }

  function select(event, index, track) {
    setCursor(index);
    pending.current = null;
    kept.current = { index: null, at: 0 };
    if (event.shiftKey && anchor.current !== null) {
      selectRange(anchor.current, index, event.ctrlKey || event.metaKey ? selected : []);
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

  /** Scroll so that this row is in view (the page header and the table's own sticky header cover the top). */
  function scrollToRow(index) {
    const scroller = root.current?.closest('.view');
    if (!scroller || !body.current) return;
    const top = body.current.getBoundingClientRect().top - scroller.getBoundingClientRect().top + index * rowHeight;
    if (top < HEADER_HEIGHT) scroller.scrollTop += top - HEADER_HEIGHT;
    else if (top + rowHeight > scroller.clientHeight) scroller.scrollTop += top + rowHeight - scroller.clientHeight;
  }

  /** Put the cursor on a row. Selecting follows it, unless `keep` (Ctrl) says to leave the selection alone, or `extend` (Shift) to stretch it. */
  function moveCursor(to, { extend = false, keep = false } = {}) {
    kept.current = { index: null, at: 0 };
    setCursor(to);
    scrollToRow(to);
    if (keep) return;
    if (extend) {
      if (anchor.current === null) anchor.current = cursor ?? to;
      selectRange(anchor.current, to, []);
      return;
    }
    anchor.current = to;
    const track = itemAt(to);
    pending.current = track ? null : to;
    setSelected(new Set(track ? [track.id] : []));
  }

  function menuForCursor() {
    const track = cursor === null ? null : itemAt(cursor);
    if (!track) return;
    const box = document.getElementById(rowId(cursor))?.getBoundingClientRect();
    rowMenu({ clientX: (box?.left ?? 0) + 48, clientY: (box?.bottom ?? 0) - 6 }, cursor, track);
  }

  function keyDown(event) {
    if (event.target.closest('input, textarea, select')) return;
    const { key } = event;
    const control = event.target !== event.currentTarget && event.target.closest('button, a'); // Enter and Space are a focused button's or link's
    const modifier = event.ctrlKey || event.metaKey;
    const last = (total ?? 0) - 1;
    const clamp = (index) => Math.min(Math.max(index, 0), last);
    const here = cursor ?? Math.min(firstVisible.current, Math.max(last, 0));
    const handled = () => {
      event.preventDefault();
      event.stopPropagation(); // the arrows and Shift+arrows are also the player's volume and seek keys
      event.currentTarget.focus({ preventScroll: true });
    };
    const page = Math.max(1, Math.floor(((root.current?.closest('.view')?.clientHeight ?? 600) - HEADER_HEIGHT) / rowHeight) - 1);
    const steps = { ArrowDown: 1, ArrowUp: -1, PageDown: page, PageUp: -page };

    if (last >= 0 && Object.hasOwn(steps, key) && !event.altKey && !event.metaKey) {
      handled();
      moveCursor(clamp(cursor === null ? here : here + steps[key]), { extend: event.shiftKey, keep: event.ctrlKey }); // the first key lands on the row at the top
    } else if (last >= 0 && (key === 'Home' || key === 'End')) {
      handled();
      moveCursor(key === 'Home' ? 0 : last, { extend: event.shiftKey, keep: event.ctrlKey && !event.shiftKey });
    } else if (last >= 0 && reorderable && event.altKey && (key === 'ArrowDown' || key === 'ArrowUp') && cursor !== null) { // not while the list reloads after a move
      handled();
      const to = clamp(cursor + steps[key]);
      if (to === cursor) return;
      kept.current = { index: to, at: Date.now() };
      setCursor(to);
      scrollToRow(to);
      onReorder?.(cursor, to);
    } else if (key === 'Enter' && !control) {
      let index = cursor;
      if (index === null && selected.size === 1) for (let i = 0; i < (total ?? 0); i++) if (selected.has(itemAt(i)?.id)) index = i;
      if (index === null) return;
      handled();
      onPlay?.(index);
    } else if (key === ' ' && modifier && cursor !== null && itemAt(cursor)) {
      handled();
      const next = new Set(selected);
      const id = itemAt(cursor).id;
      next.has(id) ? next.delete(id) : next.add(id);
      anchor.current = cursor;
      setSelected(next);
    } else if ((key === 'a' || key === 'A') && modifier) {
      event.preventDefault();
      const all = new Set();
      for (const items of cache.current.pages.values()) items.forEach((t) => all.add(t.id));
      setSelected(all);
    } else if (key === 'Delete' && onRemove && selected.size) {
      onRemove(loadedSelection());
    } else if (key === 'Escape') {
      setSelected(new Set());
    } else if (key === 'ContextMenu' || (key === 'F10' && event.shiftKey)) {
      handled();
      menuForCursor();
    } else if (!modifier && !event.altKey && !control && /^[0-5]$/.test(key)) {
      const targets = selected.size ? loadedSelection() : cursor !== null && itemAt(cursor) ? [itemAt(cursor)] : [];
      if (!targets.length) return;
      handled();
      const stars = Number(key);
      setRating(targets, stars)
        .then(() => {
          patchLoaded(targets.map((t) => t.id), { rating: stars });
          const what = targets.length === 1 ? `“${targets[0].title}”` : fmt.plural(targets.length, 'song');
          toast(stars ? `Rated ${what}: ${fmt.plural(stars, 'star')}` : `Rating cleared for ${what}`, 'success', 2200);
        })
        .catch(notifyError);
    }
  }

  const gridTemplate = cols.map((c) => COLUMNS[c].width).join(' ');
  const selectedTracks = selected.size ? loadedSelection() : [];
  const activeId = cursor !== null && total !== null && cursor < total && cursor >= range.first && cursor <= range.last ? rowId(cursor) : undefined;

  function renderRow(index) {
    const track = itemAt(index);
    const style = { top: index * rowHeight + 'px', height: rowHeight + 'px', gridTemplateColumns: gridTemplate };
    if (!track) {
      return html`<div class="trow skeleton" role="row" aria-hidden="true" key=${'s' + index} style=${style}>${cols.map((c) => html`<div role="gridcell" class="tcell ${c}"><span class="bar"></span></div>`)}</div>`;
    }
    const isCurrent = track.id === currentId;
    const isSelected = selected.has(track.id);
    const cell = {
      num: html`<div role="gridcell" class="tcell num">
        <span class="num-text">${isCurrent && playing ? html`<span class="eq-bars"><i></i><i></i><i></i></span>` : numbering === 'track' ? track.track || '' : index + 1}</span>
        <button class="row-play" tabIndex="-1" aria-label="Play ${track.title}" onClick=${(e) => { e.stopPropagation(); isCurrent ? toggle() : onPlay?.(index); }}>
          <${Icon} name=${isCurrent && playing ? 'pause' : 'play'} size=${16} />
        </button>
      </div>`,
      title: html`<div role="gridcell" class="tcell title">
        ${thumbs && html`<${Cover} trackId=${track.id} name=${track.album} size=40 />`}
        <div class="title-text">
          <span class="t1">${track.title}</span>
          ${!cols.includes('artist') && html`<span class="t2">${track.artist}</span>`}
        </div>
      </div>`,
      artist: html`<div role="gridcell" class="tcell artist"><a href="#/artist/${enc(track.album_artist || track.artist)}" tabIndex="-1" onClick=${(e) => e.stopPropagation()}>${track.artist}</a></div>`,
      album: html`<div role="gridcell" class="tcell album"><a href="#/album/${track.album_key}" tabIndex="-1" onClick=${(e) => e.stopPropagation()}>${track.album}</a></div>`,
      genre: html`<div role="gridcell" class="tcell dim">${track.genre || ''}</div>`,
      year: html`<div role="gridcell" class="tcell dim">${track.year || ''}</div>`,
      plays: html`<div role="gridcell" class="tcell dim">${track.plays || ''}</div>`,
      added: html`<div role="gridcell" class="tcell dim">${fmt.date(track.added)}</div>`,
      rating: html`<div role="gridcell" class="tcell"><${Rating} value=${track.rating} onChange=${(r) => setRating([track], r).then(() => patchLoaded([track.id], { rating: r })).catch(notifyError)} /></div>`,
      time: html`<div role="gridcell" class="tcell dim time">${fmt.time(track.duration)}</div>`,
      heart: html`<div role="gridcell" class="tcell">
        <button class="icon-btn heart ${track.favorite ? 'on' : ''}" tabIndex="-1" aria-label=${track.favorite ? 'Remove from favorites' : 'Add to favorites'} aria-pressed=${track.favorite}
          onClick=${(e) => { e.stopPropagation(); setFavorite([track], !track.favorite).then(() => patchLoaded([track.id], { favorite: !track.favorite })).catch(notifyError); }}>
          <${Icon} name="heart" size=${16} fill=${track.favorite} />
        </button></div>`,
      more: html`<div role="gridcell" class="tcell"><button class="icon-btn" tabIndex="-1" aria-label="More actions" onClick=${(e) => {
        e.stopPropagation();
        const box = e.currentTarget.getBoundingClientRect();
        rowMenu({ clientX: box.left, clientY: box.bottom + 4 }, index, track);
      }}><${Icon} name="more" size=${16} /></button></div>`,
    };
    return html`<div
      class="trow ${isCurrent ? 'current' : ''} ${isSelected ? 'selected' : ''} ${cursor === index ? 'cursor' : ''} ${dropAt === index ? 'drop-before' : ''}"
      role="row"
      id=${rowId(index)}
      aria-rowindex=${index + 2}
      aria-selected=${isSelected}
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

  return html`<div class="ttable" ref=${root}>
    ${selected.size > 1 && html`<div class="selection-bar" role="toolbar" aria-label="Selected songs" style=${{ height: HEADER_HEIGHT + 'px', marginBottom: -HEADER_HEIGHT + 'px' }}>
        <strong>${fmt.plural(selected.size, 'song')} selected</strong>
        <button class="chip-btn" onClick=${() => playQueue(selectedTracks, 0)}><${Icon} name="play" size=${14} /> Play</button>
        <button class="chip-btn" onClick=${() => { addToQueue(selectedTracks); toast(`Added ${fmt.plural(selectedTracks.length, 'song')} to the queue`); }}><${Icon} name="queue" size=${14} /> Queue</button>
        <button class="chip-btn" onClick=${() => openAddToPlaylist(selectedTracks)}><${Icon} name="plus" size=${14} /> Playlist</button>
        <button class="chip-btn" onClick=${() => openTagEditor(selectedTracks.map((t) => t.id))}><${Icon} name="edit" size=${14} /> Edit tags</button>
        <button class="chip-btn" onClick=${() => setFavorite(selectedTracks, true).then(() => patchLoaded(selectedTracks.map((t) => t.id), { favorite: true })).catch(notifyError)}><${Icon} name="heart" size=${14} /> Favorite</button>
        ${onRemove && html`<button class="chip-btn danger" onClick=${() => onRemove(selectedTracks)}><${Icon} name="trash" size=${14} /> Remove</button>`}
        <span class="spacer"></span>
        <button class="chip-btn" onClick=${() => setSelected(new Set())}>Clear</button>
      </div>`}
    <div class="tgrid" role="grid" aria-label=${label} aria-rowcount=${total === null ? -1 : total + 1} aria-colcount=${cols.length} aria-multiselectable="true"
      aria-busy=${total === null} aria-activedescendant=${activeId} tabIndex="0" onKeyDown=${keyDown}
      onFocus=${(e) => cursor === null && total && e.currentTarget.matches(':focus-visible') && setCursor(Math.min(firstVisible.current, total - 1))}>
      <div class="thead" role="row" aria-rowindex="1" style=${{ gridTemplateColumns: gridTemplate, height: HEADER_HEIGHT + 'px' }}>
        ${cols.map((c) => {
          const col = COLUMNS[c];
          const sortable = Boolean(col.sort && onSort);
          const active = Boolean(sort && col.sort && sort.key === col.sort);
          // A header with no text of its own (the heart, the menu) says what it is to a screen reader.
          const content = html`${c === 'time' ? html`<${Icon} name="clock" size=${14} title="Length" />` : col.label || html`<span class="sr-only">${col.name}</span>`}${active && html`<${Icon} name=${sort.dir === 'asc' ? 'chevron-up' : 'chevron-down'} size=${13} />`}`;
          return html`<div role="columnheader" class="hcell ${c} ${sortable ? 'sortable' : ''} ${active ? 'active' : ''}"
            aria-sort=${sortable ? (active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none') : undefined} aria-label=${sortable || !col.label ? undefined : col.name}>
            ${sortable ? html`<button type="button" class="hbtn" aria-label=${col.name} onClick=${() => onSort(col.sort)}>${content}</button>` : content}
          </div>`;
        })}
      </div>
      ${total !== 0 && html`<div class="tbody" role="rowgroup" ref=${body} style=${{ height: (total ?? 8) * rowHeight + 'px' }}>${total === null ? Array.from({ length: 8 }, (_, i) => renderRow(i)) : rows}</div>`}
    </div>
    ${total === 0 && empty}
    <div class="sr-only" role="status" aria-live="polite">${selected.size > 1 ? `${fmt.plural(selected.size, 'song')} selected` : ''}</div>
  </div>`;
}
