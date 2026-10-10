// Browsing the library: home, songs, albums, artists, search.
import { api, fmt, html, useAsync, useCallback, useDebounced, useEffect, useMemo, useRef, useState, useStore, viewerOffset } from '../lib.js';
import { Icon } from '../icons.js';
import { AlbumCard, ArtistCard, Button, Cover, Empty, InfiniteSentinel, PageHeader, PlaylistCard, Shelf, Spinner, TrackCard } from '../components.js';
import { addMusicFolder } from '../dialogs.js';
import { MixCard, playMix } from './mixes.js';
import { emptyFilters, FilterBar, filtersToRules } from '../filters.js';
import { addToQueue, playQueue } from '../player.js';
import { openTagEditor } from '../tagtools.js';
import { enc, go, href, library, notifyError, route, toast } from '../state.js';
import { staticFetch, TrackTable } from '../tracks.js';

// ------------------------------------------------------------ playing from a list

/** Queue every song matching `params` (the same filters the list shows) starting at `index`. */
export async function playMatching(params, index = 0, { shuffle, source } = {}) {
  try {
    const { ids } = await api('/tracks/ids', { params });
    if (!ids.length) return toast('Nothing to play here yet');
    const { items } = await api('/tracks/by-ids', { method: 'POST', body: { ids } });
    await playQueue(items, Math.min(index, items.length - 1), { shuffle, source });
  } catch (error) {
    notifyError(error);
  }
}

export async function playAlbum(key, options = {}) {
  return playMatching({ album: key, sort: 'artist' }, 0, { source: 'Album', ...options });
}

function EmptyLibrary() {
  return html`<${Empty} icon="folder" title="Add your music">
    <p>Choose the folder where your music lives. Music Toolkit reads the tags of the files in it (and its subfolders) and never changes or moves them unless you ask it to.</p>
    <${Button} kind="primary" icon="folder" onClick=${addMusicFolder}>Add music folder<//>
  <//>`;
}

function useLibraryRev() {
  return useStore(library, (s) => s.rev);
}

// ------------------------------------------------------------ home

function greeting() {
  const hour = new Date().getHours();
  return hour < 5 ? 'Late night listening' : hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
}

export function HomeView() {
  const rev = useLibraryRev();
  const { data, loading, error } = useAsync(() => api('/home'), [rev]);
  const { data: mixes } = useAsync(() => api('/mixes', { params: { tz: viewerOffset() } }).catch(() => ({ items: [] })), [rev]);
  if (loading && !data) return html`<${Spinner} />`;
  if (error) return html`<${Empty} icon="alert" title="Couldn't load your library">${error.message}<//>`;
  if (!data.totals.tracks) return html`<${EmptyLibrary} />`;

  const playTrackFrom = (list, source) => (track) => playQueue(list, list.findIndex((t) => t.id === track.id), { source });
  return html`
    <${PageHeader} title=${greeting()} subtitle="${fmt.plural(data.totals.tracks, 'song')} · ${fmt.plural(data.totals.plays, 'play')} logged" >
      <${Button} icon="shuffle" onClick=${() => playMatching({ sort: 'artist' }, 0, { shuffle: true, source: 'Library' })}>Shuffle everything<//>
    <//>
    ${data.recent.length > 0 && html`<${Shelf} title="Jump back in" to="/recent">
      ${data.recent.map((t) => html`<${TrackCard} key=${t.id} track=${t} onPlay=${playTrackFrom(data.recent, 'Recently played')} />`)}<//>`}
    ${mixes?.items.length > 0 && html`<${Shelf} title="Made for you" subtitle="new every day" to="/mixes">
      ${mixes.items.map((m) => html`<${MixCard} key=${m.id} mix=${m} onPlay=${() => playMix(m.id)} />`)}<//>`}
    ${data.recently_added.length > 0 && html`<${Shelf} title="Recently added" to="/albums?sort=added">
      ${data.recently_added.map((a) => html`<${AlbumCard} key=${a.key} album=${a} onPlay=${(al) => playAlbum(al.key)} />`)}<//>`}
    ${data.favorites.length > 0 && html`<${Shelf} title="Your favorites" to="/favorites">
      ${data.favorites.map((t) => html`<${TrackCard} key=${t.id} track=${t} onPlay=${playTrackFrom(data.favorites, 'Favorites')} />`)}<//>`}
    ${data.most_played.length > 0 && html`<${Shelf} title="On repeat" subtitle="your most played">
      ${data.most_played.map((t) => html`<${TrackCard} key=${t.id} track=${t} onPlay=${playTrackFrom(data.most_played, 'Most played')} />`)}<//>`}
    ${data.random_albums.length > 0 && html`<${Shelf} title="Something different" subtitle="albums to rediscover">
      ${data.random_albums.map((a) => html`<${AlbumCard} key=${a.key} album=${a} onPlay=${(al) => playAlbum(al.key)} />`)}<//>`}
    ${data.recent.length === 0 && html`<p class="hint-card"><${Icon} name="info" size=${16} /> Play something and your listening history starts building here: recently played, most played, and later your own stats.</p>`}`;
}

// ------------------------------------------------------------ songs

function presetFor(kind, artist) {
  const filters = emptyFilters();
  if (kind === 'favorites') filters.favorite = true;
  if (artist) filters.custom = [{ field: 'artist', op: 'is', value: artist }];
  return filters;
}

const DEFAULT_SORT_DIR = { plays: 'desc', added: 'desc', rating: 'desc', last_played: 'desc' };

export function SongsView({ preset }) {
  const rev = useLibraryRev();
  const { query } = useStore(route);
  const [q, setQ] = useState('');
  const debouncedQ = useDebounced(q, 250);
  const [filters, setFilters] = useState(() => presetFor(preset, query.artist));
  const [sort, setSort] = useState(
    preset === 'recent' ? { key: 'last_played', dir: 'desc' } : preset === 'added' ? { key: 'added', dir: 'desc' } : { key: 'artist', dir: 'asc' },
  );
  const [total, setTotal] = useState(null);
  const [libraryEmpty, setLibraryEmpty] = useState(false);

  useEffect(() => { setFilters(presetFor(preset, query.artist)); }, [preset, query.artist]);
  useEffect(() => { api('/about').then((a) => setLibraryEmpty(a.tracks === 0)).catch(() => {}); }, [rev]);

  const rules = useMemo(() => {
    const base = filtersToRules(filters);
    if (preset === 'recent') return { match: 'all', rules: [...(base?.rules || []), { field: 'unplayed', op: 'is', value: false }] };
    return base;
  }, [filters, preset]);
  const params = { q: debouncedQ, sort: sort.key, dir: sort.dir, rules: rules ? JSON.stringify(rules) : undefined };
  const resetKey = JSON.stringify(params) + ':' + rev;
  const fetchPage = useCallback((offset, limit) => api('/tracks', { params: { ...params, offset, limit } }), [resetKey]);
  const onSort = (key) => setSort((s) => (s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: DEFAULT_SORT_DIR[key] || 'asc' }));

  const titles = { favorites: 'Favorites', recent: 'Recently played', added: 'Recently added' };
  const filtered = debouncedQ || (rules?.rules.length ?? 0) > 0;
  return html`
    <${PageHeader} title=${titles[preset] || 'Songs'} subtitle=${total === null ? '' : filtered ? `${fmt.plural(total, 'matching song')}` : fmt.plural(total, 'song')}>
      <${Button} kind="primary" icon="play" disabled=${!total} onClick=${() => playMatching(params, 0, { source: titles[preset] || 'Songs' })}>Play<//>
      <${Button} icon="shuffle" disabled=${!total} onClick=${() => playMatching(params, 0, { shuffle: true, source: titles[preset] || 'Songs' })}>Shuffle<//>
    <//>
    <${FilterBar} q=${q} onQ=${setQ} filters=${filters} onFilters=${setFilters} placeholder="Search songs, artists, albums" rulesForSave=${rules} />
    <${TrackTable}
      fetchPage=${fetchPage} resetKey=${resetKey} sort=${sort} onSort=${onSort} onTotal=${setTotal}
      columns=${['num', 'title', 'artist', 'album', 'genre', 'year', 'plays', 'added', 'rating', 'time', 'heart', 'more']}
      onPlay=${(index) => playMatching(params, index, { source: titles[preset] || 'Songs' })}
      empty=${libraryEmpty ? html`<${EmptyLibrary} />` : html`<${Empty} icon="search" title="No songs match">Try a different search or clear a filter.<//>`}
    />`;
}

// ------------------------------------------------------------ albums

const ALBUM_SORTS = [['name', 'Album'], ['artist', 'Artist'], ['year', 'Year'], ['added', 'Recently added'], ['tracks', 'Track count']];

export function AlbumsView() {
  const rev = useLibraryRev();
  const { query } = useStore(route);
  const [q, setQ] = useState('');
  const debouncedQ = useDebounced(q, 250);
  const [filters, setFilters] = useState(emptyFilters());
  const [sort, setSort] = useState(query.sort || 'name');
  const [dir, setDir] = useState(query.sort === 'added' ? 'desc' : 'asc');
  const [state, setState] = useState({ items: [], total: null, loading: true, done: false });
  const stateRef = useRef(state);
  stateRef.current = state;
  const rules = filtersToRules(filters);
  const key = JSON.stringify([debouncedQ, sort, dir, rules, rev]);

  const load = useCallback(async (reset) => {
    setState((s) => ({ ...s, loading: true }));
    try {
      const offset = reset ? 0 : stateRef.current.items.length;
      const page = await api('/albums', { params: { q: debouncedQ, sort, dir, rules: rules ? JSON.stringify(rules) : undefined, offset, limit: 120 } });
      setState((s) => {
        const items = reset ? page.items : [...s.items, ...page.items];
        return { items, total: page.total, loading: false, done: items.length >= page.total };
      });
    } catch (error) {
      setState((s) => ({ ...s, loading: false, done: true }));
      notifyError(error);
    }
  }, [key]);
  useEffect(() => { load(true); }, [key]);

  return html`
    <${PageHeader} title="Albums" subtitle=${state.total === null ? '' : fmt.plural(state.total, 'album')} />
    <${FilterBar} q=${q} onQ=${setQ} filters=${filters} onFilters=${setFilters} placeholder="Search albums"
      extra=${html`<label class="sort-select"><span>Sort by</span>
        <select value=${sort} onChange=${(e) => { setSort(e.target.value); setDir(e.target.value === 'added' ? 'desc' : 'asc'); }}>${ALBUM_SORTS.map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select>
        <button class="icon-btn" aria-label=${dir === 'asc' ? 'Ascending' : 'Descending'} onClick=${() => setDir(dir === 'asc' ? 'desc' : 'asc')}><${Icon} name=${dir === 'asc' ? 'chevron-up' : 'chevron-down'} size=${16} /></button></label>`} />
    ${state.total === 0
      ? html`<${Empty} icon="disc" title="No albums found">${filters && rules ? 'Try clearing a filter.' : 'Add some music from Settings.'}<//>`
      : html`<div class="grid">${state.items.map((a) => html`<${AlbumCard} key=${a.key} album=${a} onPlay=${(al) => playAlbum(al.key)} />`)}</div>`}
    ${state.loading && html`<${Spinner} />`}
    <${InfiniteSentinel} disabled=${state.loading || state.done} onVisible=${() => load(false)} />`;
}

export function AlbumView({ albumKey }) {
  const rev = useLibraryRev();
  const { data: album, error } = useAsync(() => api(`/albums/${albumKey}`), [albumKey, rev]);
  if (error) return html`<${Empty} icon="disc" title="Album not found">${error.message}<//>`;
  if (!album) return html`<${Spinner} />`;
  const fetchPage = useCallback(staticFetch(album.tracks), [album]);
  const play = (index, shuffle) => playQueue(album.tracks, index, { shuffle, source: `Album: ${album.album}` });
  return html`
    <div class="hero">
      <${Cover} albumKey=${album.key} name=${album.album} size=232 class="hero-art" />
      <div class="hero-text">
        <span class="eyebrow">Album</span>
        <h1>${album.album}</h1>
        <p><a href=${href(`/artist/${enc(album.artist)}`)}>${album.artist}</a>${album.year ? ` · ${album.year}` : ''}${album.genres.length ? ` · ${album.genres.join(', ')}` : ''} · ${fmt.plural(album.tracks.length, 'song')}, ${fmt.duration(album.duration)}</p>
        <div class="hero-actions">
          <${Button} kind="primary" icon="play" onClick=${() => play(0, false)}>Play<//>
          <${Button} icon="shuffle" onClick=${() => play(0, true)}>Shuffle<//>
          <${Button} icon="queue" onClick=${() => { addToQueue(album.tracks); toast(`Added "${album.album}" to the queue`); }}>Add to queue<//>
          <${Button} icon="edit" onClick=${() => openTagEditor(album.tracks.map((t) => t.id))}>Edit tags<//>
        </div>
      </div>
    </div>
    <${TrackTable} fetchPage=${fetchPage} resetKey=${album.key + rev} numbering="track" thumbs=${false}
      columns=${['num', 'title', 'plays', 'rating', 'time', 'heart', 'more']} onPlay=${(index) => play(index, false)} />`;
}

// ------------------------------------------------------------ artists

export function ArtistsView() {
  const rev = useLibraryRev();
  const [q, setQ] = useState('');
  const debouncedQ = useDebounced(q, 250);
  const [sort, setSort] = useState('name');
  const { data, loading } = useAsync(() => api('/artists', { params: { q: debouncedQ, sort, limit: 1000 } }), [debouncedQ, sort, rev]);
  return html`
    <${PageHeader} title="Artists" subtitle=${data ? fmt.plural(data.total, 'artist') : ''} />
    <${FilterBar} q=${q} onQ=${setQ} chips=${false} filters=${emptyFilters()} onFilters=${() => {}} placeholder="Search artists"
      extra=${html`<label class="sort-select"><span>Sort by</span><select value=${sort} onChange=${(e) => setSort(e.target.value)}>
        <option value="name">Name</option><option value="tracks">Most songs</option><option value="albums">Most albums</option></select></label>`} />
    ${loading && !data ? html`<${Spinner} />` : data.items.length === 0
      ? html`<${Empty} icon="user" title="No artists found" />`
      : html`<div class="grid">${data.items.map((a) => html`<${ArtistCard} key=${a.name} artist=${a} />`)}</div>`}`;
}

export function ArtistView({ name }) {
  const rev = useLibraryRev();
  const { data: artist, error } = useAsync(() => api('/artist', { params: { name } }), [name, rev]);
  if (error) return html`<${Empty} icon="user" title="Artist not found">${error.message}<//>`;
  if (!artist) return html`<${Spinner} />`;
  const top = artist.top_tracks;
  return html`
    <div class="hero">
      <${Cover} trackId=${top[0]?.id} name=${artist.name} size=232 round class="hero-art" />
      <div class="hero-text">
        <span class="eyebrow">Artist</span>
        <h1>${artist.name}</h1>
        <p>${fmt.plural(artist.albums.length, 'album')} · ${fmt.plural(artist.tracks, 'song')}</p>
        <div class="hero-actions">
          <${Button} kind="primary" icon="play" onClick=${() => playMatching({ artist: artist.name }, 0, { source: artist.name })}>Play<//>
          <${Button} icon="shuffle" onClick=${() => playMatching({ artist: artist.name }, 0, { shuffle: true, source: artist.name })}>Shuffle<//>
          <${Button} icon="list" onClick=${() => go(`/songs?artist=${enc(artist.name)}`)}>All songs<//>
        </div>
      </div>
    </div>
    ${top.some((t) => t.plays > 0) && html`<h2 class="section-title">Most played</h2>
      <${TrackTable} fetchPage=${staticFetch(top.filter((t) => t.plays > 0).slice(0, 5))} resetKey=${'top' + artist.name + rev}
        columns=${['num', 'title', 'album', 'plays', 'time', 'heart', 'more']} onPlay=${(i) => playQueue(top.filter((t) => t.plays > 0), i, { source: artist.name })} />`}
    <h2 class="section-title">Albums</h2>
    <div class="grid">${artist.albums.map((a) => html`<${AlbumCard} key=${a.key} album=${a} onPlay=${(al) => playAlbum(al.key)} />`)}</div>`;
}

// ------------------------------------------------------------ search

export function SearchView({ term }) {
  const debounced = useDebounced(term, 150);
  const { data, loading } = useAsync(() => (debounced.trim() ? api('/search', { params: { q: debounced, limit: 12 } }) : null), [debounced]);
  if (!debounced.trim()) return html`<${Empty} icon="search" title="Search your library">Songs, albums, artists and playlists.<//>`;
  if (!data) return loading ? html`<${Spinner} />` : null;
  const nothing = !data.tracks.length && !data.albums.length && !data.artists.length && !data.playlists.length;
  if (nothing) return html`<${Empty} icon="search" title="Nothing found for “${debounced}”">Check the spelling or try fewer words.<//>`;
  return html`
    <${PageHeader} title="Results for “${debounced}”" />
    ${data.tracks.length > 0 && html`<h2 class="section-title">Songs</h2>
      <${TrackTable} fetchPage=${staticFetch(data.tracks)} resetKey=${'s' + debounced} columns=${['num', 'title', 'artist', 'album', 'time', 'heart', 'more']}
        onPlay=${(i) => playQueue(data.tracks, i, { source: 'Search' })} />`}
    ${data.albums.length > 0 && html`<h2 class="section-title">Albums</h2><div class="grid">${data.albums.map((a) => html`<${AlbumCard} key=${a.key} album=${a} onPlay=${(al) => playAlbum(al.key)} />`)}</div>`}
    ${data.artists.length > 0 && html`<h2 class="section-title">Artists</h2><div class="grid">${data.artists.map((a) => html`<${ArtistCard} key=${a.name} artist=${{ ...a, albums: undefined }} />`)}</div>`}
    ${data.playlists.length > 0 && html`<h2 class="section-title">Playlists</h2><div class="pill-list">${data.playlists.map((p) => html`<a class="pill" href=${href(`/playlist/${p.id}`)}><${Icon} name=${p.kind === 'smart' ? 'sparkles' : 'list'} size=${14} /> ${p.name}</a>`)}</div>`}`;
}
