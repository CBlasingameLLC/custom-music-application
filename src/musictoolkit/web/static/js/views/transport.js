// The controls that drive playback, shared by the player bar of a computer and the full-screen player of a phone.
import { fmt, html, useState, useStore } from '../lib.js';
import { Icon } from '../icons.js';
import { IconButton } from '../components.js';
import { cycleRepeat, next, player, previous, seek, seekBy, SKIP_BACK, SKIP_FORWARD, toggle, toggleShuffle } from '../player.js';
import { isEpisode, isStation, isTrack } from '../items.js';
import { setStationFavorite } from '../radio.js';
import { setFavorite } from '../tracks.js';
import { notifyError } from '../state.js';

export function Seek({ track }) {
  const position = useStore(player, (s) => s.position);
  const duration = useStore(player, (s) => s.duration || track?.duration || 0);
  const [dragging, setDragging] = useState(null);
  const shown = dragging ?? position;
  const pct = duration ? Math.min(100, (shown / duration) * 100) : 0;
  // A station is live: there is no length and nothing to seek in.
  if (track && isStation(track)) return html`<div class="seek live"><span class="live-badge">LIVE</span><span class="subtle">${fmt.time(position)} listened</span></div>`;
  return html`<div class="seek">
    <span class="time">${fmt.time(shown)}</span>
    <input class="slider" type="range" min="0" max=${duration || 0} step="0.1" value=${shown} disabled=${!track} aria-label="Seek"
      aria-valuetext=${`${fmt.time(shown)} of ${fmt.time(duration)}`} style=${{ '--pct': pct + '%' }}
      onInput=${(e) => setDragging(Number(e.target.value))}
      onChange=${(e) => { seek(Number(e.target.value)); setDragging(null); }} />
    <span class="time">${fmt.time(duration)}</span>
  </div>`;
}

/** A station is kept among the favorites with the heart; an episode has none; a song has its own. */
export function Heart({ item }) {
  if (isStation(item)) {
    return html`<button class="icon-btn heart ${item.favorite ? 'on' : ''}" aria-label=${item.favorite ? 'Remove from favorites' : 'Add to favorites'} aria-pressed=${Boolean(item.favorite)}
      onClick=${() => setStationFavorite(item, !item.favorite).catch(notifyError)}><${Icon} name="heart" size=${18} fill=${Boolean(item.favorite)} /></button>`;
  }
  if (!isTrack(item)) return null;
  return html`<button class="icon-btn heart ${item.favorite ? 'on' : ''}" aria-label=${item.favorite ? 'Remove from favorites' : 'Add to favorites'} aria-pressed=${item.favorite}
    onClick=${() => setFavorite([item], !item.favorite).catch(notifyError)}><${Icon} name="heart" size=${18} fill=${item.favorite} /></button>`;
}

/** A button that skips through an episode by a few seconds. */
function Skip({ seconds }) {
  const back = seconds < 0;
  const label = `${back ? 'Back' : 'Forward'} ${Math.abs(seconds)} seconds`;
  return html`<button class="icon-btn skip" title=${label} aria-label=${label} onClick=${() => seekBy(seconds)}>
    <${Icon} name=${back ? 'rewind' : 'fast-forward'} size=${18} /><span class="skip-n" aria-hidden="true">${Math.abs(seconds)}</span></button>`;
}

/** The buttons that drive playback: shuffle (or skip back, for an episode), previous, play and pause, next, repeat (or skip forward). `big` is the phone's full-screen player. */
export function Transport({ s, track, big = false }) {
  const episode = Boolean(track) && isEpisode(track);
  const station = Boolean(track) && isStation(track);
  const size = big ? 28 : 20;
  return html`<div class="controls ${big ? 'big' : ''}">
    ${episode ? html`<${Skip} seconds=${-SKIP_BACK} />` : station ? html`<span class="ctl-gap"></span>` : html`<${IconButton} icon="shuffle" title=${s.shuffle ? 'Shuffle: on' : 'Shuffle: off'} active=${s.shuffle} onClick=${toggleShuffle} />`}
    <${IconButton} icon="prev" title="Previous" size=${size} onClick=${previous} />
    <button class="play-btn" aria-label=${s.playing ? 'Pause' : 'Play'} disabled=${!track} onClick=${toggle}>
      ${s.loading && (s.playing || station) ? html`<span class="spinner small"></span>` : html`<${Icon} name=${s.playing ? 'pause' : 'play'} size=${big ? 30 : 22} />`}
    </button>
    <${IconButton} icon="next" title="Next" size=${size} onClick=${next} />
    ${episode ? html`<${Skip} seconds=${SKIP_FORWARD} />` : station ? html`<span class="ctl-gap"></span>` : html`<${IconButton} icon=${s.repeat === 'one' ? 'repeat-one' : 'repeat'} title=${`Repeat: ${s.repeat}`} active=${s.repeat !== 'off'} onClick=${cycleRepeat} />`}
  </div>`;
}
