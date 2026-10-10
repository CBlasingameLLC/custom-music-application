// Mixes: ready-made queues from your library and your listening. They are drawn fresh each day and kept for the day,
// so a card keeps its cover and opening it twice gives the same songs.
import { api, fmt, html, useAsync, useCallback, useStore, viewerOffset } from '../lib.js';
import { Icon } from '../icons.js';
import { Button, Cover, Empty, PageHeader, Spinner } from '../components.js';
import { promptText } from '../dialogs.js';
import { playQueue } from '../player.js';
import { go, href, library, loadPlaylists, notifyError, toast } from '../state.js';
import { staticFetch, TrackTable } from '../tracks.js';

/** Queue a mix in its own order (or shuffled). */
export async function playMix(id, { shuffle = false } = {}) {
  try {
    const mix = await api(`/mixes/${id}`, { params: { tz: viewerOffset() } });
    await playQueue(mix.tracks, 0, { shuffle, source: `Mix: ${mix.title}` });
  } catch (error) {
    notifyError(error);
  }
}

export function MixCard({ mix, onPlay }) {
  return html`<a class="card mix-card" href=${href(`/mix/${mix.id}`)} title=${mix.subtitle}>
    <div class="card-art">
      <${Cover} trackId=${mix.cover_track_id} name=${mix.title} size=200 />
      <span class="mix-badge">Mix</span>
      <button class="card-play" aria-label="Play ${mix.title}" onClick=${(e) => { e.preventDefault(); e.stopPropagation(); onPlay(mix); }}>
        <${Icon} name="play" size=${22} />
      </button>
    </div>
    <div class="card-title">${mix.title}</div>
    <div class="card-sub">${fmt.plural(mix.count, 'song')} · ${mix.subtitle}</div>
  </a>`;
}

export function MixesView() {
  const rev = useStore(library, (s) => s.rev);
  const { data, error } = useAsync(() => api('/mixes', { params: { tz: viewerOffset() } }), [rev]);
  return html`
    <${PageHeader} title="Made for you" subtitle="Mixes drawn from your library and your listening. They change every day." />
    ${error ? html`<${Empty} icon="alert" title="The mixes could not be made">${error.message}<//>`
      : !data ? html`<${Spinner} />`
      : data.items.length === 0
        ? html`<${Empty} icon="sparkles" title="No mixes yet">
            <p>Mixes appear once there is enough to draw from: a library of a few dozen songs, and some listening. Songs you play here are logged, and your Spotify history can be imported.</p>
            <${Button} icon="upload" onClick=${() => go('/history/import')}>Import Spotify history<//>
          <//>`
        : html`<div class="grid">${data.items.map((m) => html`<${MixCard} key=${m.id} mix=${m} onPlay=${() => playMix(m.id)} />`)}</div>`}`;
}

export function MixView({ id }) {
  const rev = useStore(library, (s) => s.rev);
  const { data: mix, error } = useAsync(() => api(`/mixes/${id}`, { params: { tz: viewerOffset() } }), [id, rev]);
  const fetchPage = useCallback(staticFetch(mix?.tracks || []), [mix]);
  if (error) return html`<${Empty} icon="sparkles" title="That mix is empty today">
    <p>${error.message}</p><a class="btn" href=${href('/mixes')}>See the other mixes</a><//>`;
  if (!mix) return html`<${Spinner} />`;

  const seconds = mix.tracks.reduce((sum, t) => sum + (t.duration || 0), 0);
  const play = (index, shuffle = false) => playQueue(mix.tracks, index, { shuffle, source: `Mix: ${mix.title}` });

  async function saveAsPlaylist() {
    const name = await promptText({ title: 'Save as playlist', label: 'Name', value: mix.title, confirm: 'Save' });
    if (!name) return;
    try {
      const playlist = await api('/playlists', { method: 'POST', body: { name } });
      await api(`/playlists/${playlist.id}/tracks`, { method: 'POST', body: { ids: mix.tracks.map((t) => t.id) } });
      await loadPlaylists();
      toast(`Saved "${name}" with ${fmt.plural(mix.tracks.length, 'song')}`, 'success');
      go(`/playlist/${playlist.id}`);
    } catch (error) {
      notifyError(error);
    }
  }

  return html`
    <div class="hero">
      <${Cover} trackId=${mix.tracks[0]?.id} name=${mix.title} size=232 class="hero-art" />
      <div class="hero-text">
        <span class="eyebrow">Mix · new every day</span>
        <h1>${mix.title}</h1>
        <p>${mix.subtitle}</p>
        <p class="subtle">${fmt.plural(mix.tracks.length, 'song')}, ${fmt.duration(seconds)}</p>
        <div class="hero-actions">
          <${Button} kind="primary" icon="play" onClick=${() => play(0)}>Play<//>
          <${Button} icon="shuffle" onClick=${() => play(0, true)}>Shuffle<//>
          <${Button} icon="plus" onClick=${saveAsPlaylist}>Save as playlist<//>
        </div>
      </div>
    </div>
    <${TrackTable} fetchPage=${fetchPage} resetKey=${`mix:${id}:${mix.tracks.length}:${rev}`}
      columns=${['num', 'title', 'artist', 'album', 'year', 'rating', 'time', 'heart', 'more']} onPlay=${(index) => play(index)} />`;
}
