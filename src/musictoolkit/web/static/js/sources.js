// Choosing music by what it is: the whole library, favorites, a playlist, a genre, a rating, what was added lately. Shared by the screens that take
// "some of the library": what goes on a device, and what to convert. The server resolves these the same way for both (sync/selection.py).
import { fmt, html, useEffect, useState, useStore } from './lib.js';
import { Icon } from './icons.js';
import { Button } from './components.js';
import { library, loadPlaylists } from './state.js';

const SOURCE_KINDS = [
  ['favorites', 'Favorites'],
  ['all', 'Whole library'],
  ['playlist', 'A playlist'],
  ['genre', 'A genre'],
  ['rating', 'Songs rated…'],
  ['recent', 'Recently added'],
];
const RECENT = [[7, 'the last 7 days'], [30, 'the last 30 days'], [90, 'the last 3 months'], [365, 'the last year']];

export const sourceKey = (s) => `${s.kind}:${s.id ?? s.value ?? s.min ?? s.days ?? ''}`;

export function describeSource(s, playlists) {
  switch (s.kind) {
    case 'all': return 'Whole library';
    case 'favorites': return 'Favorites';
    case 'playlist': return `Playlist: ${playlists.find((p) => p.id === s.id)?.name || 'a playlist that was deleted'}`;
    case 'genre': return `Genre: ${s.value}`;
    case 'rating': return `Rated ${s.min}+ stars`;
    default: return `Added in ${RECENT.find(([days]) => days === s.days)?.[1] || `the last ${s.days} days`}`;
  }
}

/** The chips for what is chosen, and a line to add more. `label` names the group of chips; `empty` is said while nothing is chosen. */
export function SourceEditor({ sources, onChange, disabled, label = 'What goes on this device', empty = 'Nothing chosen yet. Add the music this device should hold.' }) {
  const playlists = useStore(library, (s) => s.playlists);
  const facets = useStore(library, (s) => s.facets);
  const [kind, setKind] = useState('favorites');
  const [playlistId, setPlaylistId] = useState('');
  const [genre, setGenre] = useState('');
  const [rating, setRating] = useState('4');
  const [days, setDays] = useState('30');
  const genres = facets?.genres || [];
  useEffect(() => { loadPlaylists().catch(() => {}); }, []); // a playlist made a moment ago must be choosable

  const candidate = {
    all: { kind: 'all' },
    favorites: { kind: 'favorites' },
    playlist: playlistId ? { kind: 'playlist', id: Number(playlistId) } : null,
    genre: genre ? { kind: 'genre', value: genre } : null,
    rating: { kind: 'rating', min: Number(rating) },
    recent: { kind: 'recent', days: Number(days) },
  }[kind];
  const duplicate = candidate && sources.some((s) => sourceKey(s) === sourceKey(candidate));

  return html`<div class="source-editor">
    <div class="source-chips" role="group" aria-label=${label}>
      ${sources.length === 0 && html`<span class="subtle">${empty}</span>`}
      ${sources.map((s) => html`<span class="source-chip" key=${sourceKey(s)}>${describeSource(s, playlists)}
        <button type="button" aria-label="Remove ${describeSource(s, playlists)}" disabled=${disabled} onClick=${() => onChange(sources.filter((x) => x !== s))}><${Icon} name="x" size=${13} /></button>
      </span>`)}
    </div>
    <div class="source-add">
      <select aria-label="Kind of music to add" value=${kind} disabled=${disabled} onChange=${(e) => setKind(e.target.value)}>
        ${SOURCE_KINDS.map(([value, text]) => html`<option value=${value} key=${value}>${text}</option>`)}
      </select>
      ${kind === 'playlist' && (playlists.length
        ? html`<select aria-label="Playlist" value=${playlistId} disabled=${disabled} onChange=${(e) => setPlaylistId(e.target.value)}>
            <option value="">Choose a playlist…</option>
            ${playlists.map((p) => html`<option value=${p.id} key=${p.id}>${p.name}${p.kind === 'smart' ? ' (smart)' : ''} · ${fmt.plural(p.tracks, 'song')}</option>`)}
          </select>`
        : html`<span class="subtle">You have no playlists yet.</span>`)}
      ${kind === 'genre' && (genres.length
        ? html`<select aria-label="Genre" value=${genre} disabled=${disabled} onChange=${(e) => setGenre(e.target.value)}>
            <option value="">Choose a genre…</option>
            ${genres.map((g) => html`<option value=${g.name} key=${g.name}>${g.name} · ${fmt.plural(g.count, 'song')}</option>`)}
          </select>`
        : html`<span class="subtle">No genres in the library yet.</span>`)}
      ${kind === 'rating' && html`<select aria-label="Minimum rating" value=${rating} disabled=${disabled} onChange=${(e) => setRating(e.target.value)}>
        ${[5, 4, 3, 2, 1].map((n) => html`<option value=${n} key=${n}>${n} star${n === 1 ? '' : 's'} or more</option>`)}
      </select>`}
      ${kind === 'recent' && html`<select aria-label="How recently added" value=${days} disabled=${disabled} onChange=${(e) => setDays(e.target.value)}>
        ${RECENT.map(([value, text]) => html`<option value=${value} key=${value}>Added in ${text}</option>`)}
      </select>`}
      <${Button} small icon="plus" disabled=${disabled || !candidate || duplicate} onClick=${() => onChange([...sources, candidate])}>Add<//>
    </div>
  </div>`;
}
