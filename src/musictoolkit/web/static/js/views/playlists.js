// Playlists: the list, and one playlist (manual or smart).
import { api, fmt, html, useAsync, useCallback, useEffect, useState, useStore } from '../lib.js';
import { Icon } from '../icons.js';
import { Button, Cover, Empty, PageHeader, PlaylistCard, Spinner } from '../components.js';
import { confirmDialog, pickFile, promptText } from '../dialogs.js';
import { openSmartPlaylistEditor, describeRule } from '../filters.js';
import { go, library, loadPlaylists, notifyError, toast } from '../state.js';
import { TrackTable } from '../tracks.js';
import { playMatching } from './library.js';

const playPlaylist = (playlist, shuffle = false) =>
  playMatching({ playlist: playlist.id }, 0, { shuffle, source: `Playlist: ${playlist.name}` });

export function PlaylistsView() {
  const rev = useStore(library, (s) => s.rev);
  const playlists = useStore(library, (s) => s.playlists);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => { loadPlaylists().then(() => setLoaded(true)).catch(notifyError); }, [rev]);

  async function create() {
    const name = await promptText({ title: 'New playlist', label: 'Name', placeholder: 'e.g. Road trip', confirm: 'Create' });
    if (!name) return;
    try {
      const playlist = await api('/playlists', { method: 'POST', body: { name } });
      await loadPlaylists();
      go(`/playlist/${playlist.id}`);
    } catch (error) { notifyError(error); }
  }

  async function importM3u() {
    const path = await pickFile('Import a playlist', { filters: [{ name: 'Playlists', extensions: ['m3u', 'm3u8'] }] });
    if (!path) return;
    try {
      const result = await api('/playlists/import', { method: 'POST', body: { path } });
      toast(`Imported "${result.playlist.name}": ${result.matched} of ${result.entries} songs found in your library`, result.unmatched ? 'info' : 'success', 8000);
      await loadPlaylists();
      go(`/playlist/${result.playlist.id}`);
    } catch (error) { notifyError(error); }
  }

  return html`
    <${PageHeader} title="Playlists" subtitle=${loaded ? fmt.plural(playlists.length, 'playlist') : ''}>
      <${Button} kind="primary" icon="plus" onClick=${create}>New playlist<//>
      <${Button} icon="sparkles" onClick=${() => openSmartPlaylistEditor()}>New smart playlist<//>
      <${Button} icon="upload" onClick=${importM3u}>Import .m3u<//>
    <//>
    ${!loaded ? html`<${Spinner} />` : playlists.length === 0
      ? html`<${Empty} icon="list" title="No playlists yet">
          <p>Make a playlist from any song's menu, or let a <strong>smart playlist</strong> fill itself from rules such as “rated 4+ and not played this month”.</p>
        <//>`
      : html`<div class="grid">${playlists.map((p) => html`<${PlaylistCard} key=${p.id} playlist=${p} onPlay=${playPlaylist} />`)}</div>`}`;
}

export function PlaylistView({ id }) {
  const rev = useStore(library, (s) => s.rev);
  const [version, setVersion] = useState(0);
  const { data: playlist, error } = useAsync(() => api(`/playlists/${id}`), [id, rev, version]);
  const [sort, setSort] = useState({ key: 'position', dir: 'asc' });
  useEffect(() => setSort({ key: 'position', dir: 'asc' }), [id]);

  const params = { playlist: id, sort: sort.key, dir: sort.dir };
  const resetKey = JSON.stringify(params) + ':' + rev + ':' + version;
  const fetchPage = useCallback((offset, limit) => api('/tracks', { params: { ...params, offset, limit } }), [resetKey]);
  const onSort = (key) => setSort((s) => (key === 'track' ? { key: 'position', dir: 'asc' } : s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: 'asc' }));

  if (error) return html`<${Empty} icon="list" title="Playlist not found">${error.message}<//>`;
  if (!playlist) return html`<${Spinner} />`;
  const manual = playlist.kind === 'manual';
  const ordered = manual && sort.key === 'position';
  const refresh = () => { setVersion((v) => v + 1); loadPlaylists(); };

  async function rename() {
    const name = await promptText({ title: 'Rename playlist', label: 'Name', value: playlist.name, confirm: 'Save' });
    if (!name || name === playlist.name) return;
    try { await api(`/playlists/${id}`, { method: 'PATCH', body: { name } }); refresh(); } catch (e) { notifyError(e); }
  }
  async function remove() {
    if (!(await confirmDialog({ title: 'Delete playlist', message: `Delete "${playlist.name}"? The songs stay in your library.`, confirm: 'Delete', danger: true }))) return;
    try { await api(`/playlists/${id}`, { method: 'DELETE' }); await loadPlaylists(); go('/playlists'); toast('Playlist deleted'); } catch (e) { notifyError(e); }
  }
  async function reorder(from, to) {
    try { await api(`/playlists/${id}/tracks/move`, { method: 'POST', body: { from_position: from, to_position: to } }); refresh(); } catch (e) { notifyError(e); }
  }
  async function removeTracks(tracks) {
    try {
      await api(`/playlists/${id}/tracks/remove`, { method: 'POST', body: { positions: tracks.map((t) => t.pos) } });
      toast(`Removed ${fmt.plural(tracks.length, 'song')} from the playlist`);
      refresh();
    } catch (e) { notifyError(e); }
  }

  const ids = playlist.cover_track_ids;
  return html`
    <div class="hero">
      <div class="hero-art mosaic ${ids.length < 4 ? 'single' : ''}">
        ${ids.length === 0 ? html`<span class="cover-fallback"><${Icon} name=${manual ? 'list' : 'sparkles'} size=${56} /></span>`
          : ids.length < 4 ? html`<${Cover} trackId=${ids[0]} name=${playlist.name} size=232 />`
          : ids.map((tid) => html`<${Cover} trackId=${tid} name=${playlist.name} size=116 />`)}
      </div>
      <div class="hero-text">
        <span class="eyebrow">${manual ? 'Playlist' : 'Smart playlist'}</span>
        <h1>${playlist.name}</h1>
        <p>${fmt.plural(playlist.tracks, 'song')}${playlist.tracks ? `, ${fmt.duration(playlist.duration)}` : ''}</p>
        ${!manual && playlist.rules && html`<p class="rule-summary"><${Icon} name="sparkles" size=${14} /> Songs that match ${playlist.rules.match === 'any' ? 'any' : 'all'} of: ${playlist.rules.rules.map(describeRule).join(' · ')}</p>`}
        <div class="hero-actions">
          <${Button} kind="primary" icon="play" disabled=${!playlist.tracks} onClick=${() => playPlaylist(playlist)}>Play<//>
          <${Button} icon="shuffle" disabled=${!playlist.tracks} onClick=${() => playPlaylist(playlist, true)}>Shuffle<//>
          ${manual ? html`<${Button} icon="edit" onClick=${rename}>Rename<//>` : html`<${Button} icon="edit" onClick=${() => openSmartPlaylistEditor({ playlist })}>Edit rules<//>`}
          <a class="btn" href="/api/playlists/${id}/export.m3u8" download><${Icon} name="download" size=${17} /><span>Export .m3u8</span></a>
          <${Button} icon="trash" kind="danger-ghost" onClick=${remove}>Delete<//>
        </div>
      </div>
    </div>
    <${TrackTable} fetchPage=${fetchPage} resetKey=${resetKey} sort=${sort} onSort=${onSort}
      columns=${['num', 'title', 'artist', 'album', 'year', 'rating', 'time', 'heart', 'more']}
      reorderable=${ordered} onReorder=${reorder} onRemove=${manual ? removeTracks : undefined}
      onPlay=${(index) => playMatching({ playlist: id, sort: sort.key, dir: sort.dir }, index, { source: `Playlist: ${playlist.name}` })}
      empty=${html`<${Empty} icon="music" title="This playlist is empty">${manual ? 'Add songs from any song’s “…” menu, or select several and use “Playlist”.' : 'No songs match these rules yet.'}<//>`} />
    ${ordered && playlist.tracks > 1 && html`<p class="hint-card subtle"><${Icon} name="grip" size=${14} /> Drag songs to reorder them.</p>`}`;
}
