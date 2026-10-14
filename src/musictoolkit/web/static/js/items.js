// What the queue can hold: songs from the library, radio stations and podcast episodes. They differ in where their audio
// comes from, whether it ever ends, and what is shown for them; this is the one place that knows. (Pure functions.)

/** 'track' (a song in the library, the default), 'station' (live radio) or 'episode' (a podcast episode). */
export const kindOf = (item) => item?.kind || 'track';
export const isTrack = (item) => kindOf(item) === 'track';
export const isStation = (item) => kindOf(item) === 'station';
export const isEpisode = (item) => kindOf(item) === 'episode';

/** An item's identity: ids are only unique within a kind (song 5, station 5 and episode 5 are three different things). */
export const itemKey = (item) => `${kindOf(item)}:${item.id}`;
export const sameItem = (a, b) => Boolean(a) && Boolean(b) && itemKey(a) === itemKey(b);

/**
 * Where the audio comes from: always this app, which fetches radio and podcasts from the internet on the page's behalf.
 * `join` numbers the times a live stream is joined: the browser would answer a second load of the same address with what it already
 * holds of the first (a station that hung up would be "rejoined" by playing its old sound again), so each join asks for an address of its own.
 */
export function streamUrl(item, join = 0) {
  switch (kindOf(item)) {
    case 'station': return `/api/radio/stations/${item.id}/stream?join=${join}`;
    case 'episode': return `/api/podcasts/episodes/${item.id}/stream`;
    default: return `/api/tracks/${item.id}/stream`;
  }
}

/** The address of an item's picture (null when it has none). `size` only matters for songs: the others come in one size. */
export function artSrc(item, size = 512) {
  switch (kindOf(item)) {
    case 'station': return item.has_art ? `/api/radio/stations/${item.id}/art` : null;
    case 'episode': return item.has_art ? `/api/podcasts/episodes/${item.id}/art` : null;
    case 'podcast': return item.has_art ? `/api/podcasts/${item.id}/art` : null; // a show, not a queue item: only its picture is wanted
    default: return `/api/art/track/${item.id}?size=${size}`;
  }
}

/** The glyph drawn where there is no picture. */
export const fallbackIcon = (item) => ({ station: 'radio', episode: 'mic', podcast: 'mic' })[kindOf(item)] || 'music';

/**
 * How an item is shown: `title`, the `subtitle` under it, the `name` its colour is drawn from, and whether it is `live`.
 * `liveTitle` is what a station says is playing right now (empty when it says nothing).
 */
export function describe(item, liveTitle = '') {
  switch (kindOf(item)) {
    case 'station':
      return { title: liveTitle || item.name, subtitle: liveTitle ? item.name : 'Live radio', name: item.name, live: true };
    case 'episode':
      return { title: item.title, subtitle: item.podcast_title || 'Podcast', name: item.podcast_title || item.title, live: false };
    default:
      return { title: item.title, subtitle: item.artist, name: item.album || item.title, live: false };
  }
}

/** The page a click on an item's title goes to. */
export function pageOf(item) {
  switch (kindOf(item)) {
    case 'station': return '/radio';
    case 'episode': return `/podcast/${item.podcast_id}`;
    default: return '/now';
  }
}

/** Playback speeds offered for podcasts, and the one after `speed` (wrapping round). */
export const SPEEDS = [0.75, 1, 1.25, 1.5, 1.75, 2];
export const nextSpeed = (speed) => SPEEDS[(Math.max(SPEEDS.indexOf(speed), 0) + 1) % SPEEDS.length];
export const speedLabel = (speed) => `${speed}×`;
