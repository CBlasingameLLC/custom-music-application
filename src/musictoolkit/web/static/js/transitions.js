// How one song hands over to the next. Pure functions: the player (player.js) does the playing.

export const MAX_CROSSFADE = 12; // seconds, the longest blend offered
export const GAPLESS_BLEND = 0.03; // seconds the next song starts before the last one ends, to hide the seam
const SHARE_OF_A_SONG = 0.4; // a blend never takes more than this much of either song
const SHORTEST_CROSSFADE = 0.5; // below this a crossfade is just a seam, and is treated as gapless

/**
 * How long the end of one song blends into the start of the next, in seconds; 0 when the songs are not to be joined
 * ahead of time (the next one then starts when the last one ends, as it always has).
 *
 * `crossfade` is the length the person chose (0 = off); `gapless` says the songs should run on with no silence.
 * Short songs get a shorter blend, so a thirty-second jingle does not spend half its length fading.
 */
export function blendSeconds({ crossfade = 0, gapless = true }, outgoingDuration, incomingDuration) {
  const known = [outgoingDuration, incomingDuration].every((d) => Number.isFinite(d) && d > 0);
  if (!known) return 0;
  if (crossfade > 0) {
    const blend = Math.min(crossfade, MAX_CROSSFADE, outgoingDuration * SHARE_OF_A_SONG, incomingDuration * SHARE_OF_A_SONG);
    return blend >= SHORTEST_CROSSFADE ? blend : GAPLESS_BLEND;
  }
  return gapless ? GAPLESS_BLEND : 0;
}

/**
 * Equal-power curves for a blend, [fadeOut, fadeIn], each `points` values long. Added together the two sounds keep
 * the same loudness all the way through, which a straight line from 1 to 0 (and 0 to 1) does not: it dips in the middle.
 */
export function equalPowerCurves(points = 128) {
  const out = new Float32Array(points);
  const into = new Float32Array(points);
  for (let i = 0; i < points; i++) {
    const t = i / (points - 1);
    out[i] = Math.cos((t * Math.PI) / 2);
    into[i] = Math.sin((t * Math.PI) / 2);
  }
  return [out, into];
}

/**
 * Where the queue goes after the song that is playing: { pos } (and `order` when the queue starts over), or null at the
 * end. With `preview` set (looking ahead to get the next song ready) a queue that would start over in a new shuffled
 * order gives null, because that order is only made when the end is reached.
 */
export function planNext(state, { preview = false } = {}) {
  if (state.pos + 1 < state.order.length) return { pos: state.pos + 1 };
  if (state.repeat === 'all' && state.order.length) {
    if (preview && state.shuffle) return null;
    return { pos: 0, order: state.shuffle ? shuffledOrder(state.items.length) : state.items.map((_, i) => i) };
  }
  return null;
}

export function shuffledOrder(count, firstIndex) {
  const rest = [];
  for (let i = 0; i < count; i++) if (i !== firstIndex) rest.push(i);
  for (let i = rest.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [rest[i], rest[j]] = [rest[j], rest[i]];
  }
  return firstIndex === undefined || firstIndex < 0 ? rest : [firstIndex, ...rest];
}

/** The queue item at a planned position (`plan` as returned by planNext). */
export function itemAt(state, plan) {
  if (!plan) return null;
  const order = plan.order || state.order;
  return state.items[order[plan.pos]] || null;
}
