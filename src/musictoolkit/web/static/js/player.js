// The player: two <audio> elements ("decks") routed through one Web Audio graph, plus the queue.
//
//   deck A: <audio> -> ReplayGain -> fade \
//                                          > bus -> 10-band EQ -> master volume -> analyser -> speakers
//   deck B: <audio> -> ReplayGain -> fade /
//
// One deck plays the current song. The other is the spare: while a song plays, the next one is loaded into it, so it
// can start the moment the last one ends (gapless) or while that one is still fading out (crossfade). Each deck has
// its own `fade` gain, which is what a crossfade moves; the master volume, the EQ and the analyser are shared.
//
// The queue is `items` (what was queued) plus `order` (indices into items, in play order) and `pos` (where we are in
// `order`). Shuffle only rewrites `order`.
//
// An item is a song (the default), a radio station (`kind: 'station'`: live, so nothing to seek in, no gapless, no play log) or a
// podcast episode (`kind: 'episode'`: it resumes where listening stopped, and where it has got to is sent to the app as it plays).
// See items.js. Only songs are joined to each other ahead of time and counted in the listening history.
import { api, createStore } from './lib.js';
import { SPEEDS, describe, isEpisode, isStation, isTrack, itemKey, kindOf, sameItem, streamUrl, artSrc } from './items.js';
import { GAPLESS_BLEND, MAX_CROSSFADE, blendSeconds, equalPowerCurves, itemAt, planNext, shuffledOrder } from './transitions.js';

const STORAGE_KEY = 'mtk.player.v1';
export const EQ_BANDS = [32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000];
export const EQ_PRESETS = {
  Flat: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
  'Bass boost': [6, 5, 4, 2, 0, 0, 0, 0, 0, 0],
  'Treble boost': [0, 0, 0, 0, 0, 1, 2, 4, 5, 6],
  Vocal: [-2, -2, -1, 1, 3, 4, 3, 1, 0, -1],
  Rock: [5, 4, 2, -1, -2, -1, 2, 4, 5, 5],
  Electronic: [5, 4, 1, 0, -2, 2, 1, 2, 4, 5],
  Acoustic: [4, 3, 2, 1, 2, 2, 3, 3, 3, 2],
  Loudness: [6, 4, 0, 0, -1, 0, -1, 0, 4, 2],
};
export { MAX_CROSSFADE };

const saved = (() => {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
  } catch {
    return {};
  }
})();

export const player = createStore({
  items: [],
  order: [],
  pos: -1,
  playing: false,
  loading: false,
  position: 0,
  duration: 0,
  volume: saved.volume ?? 0.8,
  muted: false,
  shuffle: saved.shuffle ?? false,
  repeat: saved.repeat ?? 'off', // off | all | one
  error: null,
  normalize: saved.normalize ?? true,
  eq: saved.eq ?? { enabled: false, gains: [...EQ_PRESETS.Flat], preset: 'Flat' },
  crossfade: Math.min(Math.max(Number(saved.crossfade) || 0, 0), MAX_CROSSFADE), // seconds; 0 = no crossfade
  gapless: saved.gapless ?? true, // start the next song as the last one ends, with no silence between
  sleepAt: null,
  source: '',
  speed: SPEEDS.includes(saved.speed) ? saved.speed : 1, // how fast podcasts play
  liveTitle: '', // what the station playing says is on (empty when it says nothing)
});

export const current = (state = player.get()) => state.items[state.order[state.pos]] || null;

// ------------------------------------------------------------ decks

const makeDeck = (name) => {
  const el = new Audio();
  el.preload = 'auto';
  return { name, el, track: null, nodes: null, gain: null, gainFor: null, failed: false };
};
const decks = [makeDeck('a'), makeDeck('b')];
let active = decks[0]; // the deck playing (or paused on) the current song
const spare = () => (active === decks[0] ? decks[1] : decks[0]);
const loaded = (deck) => Boolean(deck.track) && !deck.failed && deck.el.readyState >= 3 && Number.isFinite(deck.el.duration);

let graph = null;
let transition = null; // while one song blends into the next: { from, to, endsAt (audio clock), before }
let listen = null; // what has been listened to for the current track, for play logging
let consecutiveErrors = 0;
let tickTimer = null;
const counts = { blends: 0, hardEnds: 0 }; // how songs changed over: for the browser tests, and anyone debugging a seam
const [FADE_OUT, FADE_IN] = equalPowerCurves();
const listeners = { 'play-logged': new Set() };

export const onPlayLogged = (fn) => {
  listeners['play-logged'].add(fn);
  return () => listeners['play-logged'].delete(fn);
};

/** What each deck is doing: for the browser tests, and for anyone debugging a seam between two songs. */
export const deckState = () => ({
  active: active.name,
  blending: Boolean(transition),
  rejoining: rejoinTimer !== null, // a dropped live stream is waiting to be joined again
  ...counts,
  decks: decks.map((d) => ({
    name: d.name,
    track: d.track ? d.track.id : null,
    src: d.el.getAttribute('src'), // the address it was told to load (null when empty)
    paused: d.el.paused,
    ended: d.el.ended,
    time: d.el.currentTime,
    duration: d.el.duration,
    ready: d.el.readyState,
    rate: d.el.playbackRate,
    fade: d.nodes ? d.nodes.fade.gain.value : null,
    levelling: d.nodes ? d.nodes.rg.gain.value : null,
  })),
});

// ------------------------------------------------------------ audio graph

function ensureGraph() {
  if (graph) return graph;
  const Context = window.AudioContext || window.webkitAudioContext;
  const ctx = new Context();
  const bus = ctx.createGain();
  for (const deck of decks) {
    const source = ctx.createMediaElementSource(deck.el);
    const rg = ctx.createGain();
    const fade = ctx.createGain();
    source.connect(rg);
    rg.connect(fade);
    fade.connect(bus);
    deck.nodes = { rg, fade };
  }
  const filters = EQ_BANDS.map((frequency, i) => {
    const filter = ctx.createBiquadFilter();
    filter.type = i === 0 ? 'lowshelf' : i === EQ_BANDS.length - 1 ? 'highshelf' : 'peaking';
    filter.frequency.value = frequency;
    filter.Q.value = 1.1;
    return filter;
  });
  const master = ctx.createGain();
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 2048;
  analyser.smoothingTimeConstant = 0.82;
  let node = bus;
  for (const next of [...filters, master, analyser]) {
    node.connect(next);
    node = next;
  }
  analyser.connect(ctx.destination);
  graph = { ctx, filters, master, analyser };
  applyVolume();
  applyEq();
  for (const deck of decks) {
    applyDeckGain(deck);
    syncGain(deck);
  }
  return graph;
}

export const getAnalyser = () => ensureGraph().analyser;

function applyVolume() {
  if (!graph) return;
  const { volume, muted } = player.get();
  graph.master.gain.value = muted ? 0 : Math.pow(volume, 2);
}

function applyEq() {
  if (!graph) return;
  const { eq } = player.get();
  graph.filters.forEach((filter, i) => {
    filter.gain.value = eq.enabled ? eq.gains[i] || 0 : 0;
  });
}

function applyDeckGain(deck) {
  if (deck.nodes && deck.gain !== null) deck.nodes.rg.gain.value = deck.gain;
}

/** Work out the loudness correction (ReplayGain) of the song on a deck, and apply it once the graph exists. */
async function syncGain(deck, force = false) {
  const track = deck.track;
  if (!track || (!force && deck.gainFor === itemKey(track))) return;
  deck.gainFor = itemKey(track);
  let gain = 1;
  if (isTrack(track) && player.get().normalize) { // radio and podcasts carry no loudness tags: they play as they come
    try {
      const info = await api(`/tracks/${track.id}/info`);
      const db = info.replaygain_track_db ?? info.replaygain_album_db;
      if (typeof db === 'number') gain = Math.min(Math.pow(10, db / 20), 1.5);
    } catch {
      /* no gain info is fine */
    }
  }
  if (sameItem(deck.track, track)) {
    deck.gain = gain;
    applyDeckGain(deck);
  }
}

const REWIND = 3; // seconds an episode starts before where listening stopped, to pick the thread up again
const RESUME_AFTER = 10; // an episode heard less than this far in starts from the beginning
let joins = 0; // how many times a live stream has been joined (each join is a new connection: see streamUrl)

/** Put an item on a deck (it starts loading at once; it plays when told to). */
function loadInto(deck, item) {
  deck.track = item;
  deck.gain = null;
  deck.gainFor = null;
  deck.failed = false;
  deck.el.src = streamUrl(item, ++joins);
  if (isEpisode(item) && item.position > RESUME_AFTER) {
    const at = item.position - REWIND;
    deck.el.addEventListener('loadedmetadata', () => { if (sameItem(deck.track, item)) deck.el.currentTime = at; }, { once: true });
  }
  syncGain(deck);
}

/** Stop a deck and empty it. */
function resetDeck(deck) {
  deck.el.pause();
  deck.el.removeAttribute('src');
  deck.el.load();
  deck.track = null;
  deck.gain = null;
  deck.gainFor = null;
  deck.failed = false;
  setFade(deck, 1);
}

/** A deck's fade back to full, whatever it was doing. */
function setFade(deck, value) {
  if (!deck.nodes) return;
  const param = deck.nodes.fade.gain;
  param.cancelScheduledValues(0);
  param.value = value;
}

// ------------------------------------------------------------ persistence

let persistTimer = null;
function saveNow() {
  clearTimeout(persistTimer);
  persistTimer = null;
  const s = player.get();
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        ids: s.items.map((t) => (isTrack(t) ? t.id : null)), // only songs come back next time: a station is live and an episode remembers its own place
        order: s.order,
        pos: s.pos,
        position: isTrack(current(s)) ? s.position : 0,
        volume: s.volume,
        shuffle: s.shuffle,
        repeat: s.repeat,
        normalize: s.normalize,
        eq: s.eq,
        crossfade: s.crossfade,
        gapless: s.gapless,
        speed: s.speed,
      }),
    );
  } catch {
    /* storage may be unavailable */
  }
}
// The player changes several times a second while a song plays (its position), so saving waits a moment after the first
// change and then saves whatever is current, rather than waiting for a quiet spell that never comes.
player.subscribe(() => {
  if (persistTimer === null) persistTimer = setTimeout(saveNow, 1000);
});
window.addEventListener('pagehide', saveNow);

export async function restoreQueue() {
  if (!saved.ids?.length) return;
  try {
    const { items } = await api('/tracks/by-ids', { method: 'POST', body: { ids: saved.ids.filter((id) => id !== null) } });
    const byId = new Map(items.map((t) => [t.id, t]));
    // Tracks removed from disk since last time drop out; remap the saved play order around them.
    const newIndex = new Map();
    const kept = [];
    saved.ids.forEach((id, oldIndex) => {
      if (byId.has(id)) {
        newIndex.set(oldIndex, kept.length);
        kept.push(byId.get(id));
      }
    });
    const order = (saved.order || []).filter((i) => newIndex.has(i)).map((i) => newIndex.get(i));
    if (!kept.length || !order.length) return;
    const pos = Math.min(Math.max(saved.pos ?? 0, 0), order.length - 1);
    player.set({ items: kept, order, pos });
    const track = kept[order[pos]];
    const deck = active;
    deck.track = track;
    deck.gainFor = null;
    deck.el.src = streamUrl(track);
    const resumeAt = saved.position || 0;
    deck.el.addEventListener('loadedmetadata', () => { if (deck.track === track) deck.el.currentTime = resumeAt; }, { once: true });
    player.set({ position: resumeAt, duration: track.duration || 0 });
    updateMediaSession(track);
  } catch {
    /* an empty queue is a fine fallback */
  }
}

// ------------------------------------------------------------ playback

async function play() {
  ensureGraph();
  try {
    await graph.ctx.resume();
    await active.el.play();
  } catch (error) {
    if (error.name !== 'AbortError') onFailure(error.message || String(error));
  }
}

function startListen(item) {
  // Only songs go into the listening history; an episode has its own record (the place where it stopped), a station has none.
  listen = item && isTrack(item)
    ? { id: item.id, startedAt: Date.now() / 1000, ms: 0, logged: false, announced: false, lastTime: 0, duration: item.duration }
    : null;
}

// ---- what stations and podcasts need beyond a song

const PROGRESS_EVERY = 10000; // ms between sending where an episode has got to
const TITLE_EVERY = 15000; // ms between asking what a station is playing
const MAX_RECONNECTS = 3; // times in a row a live stream that dropped is joined again
const STEADY_AFTER = 20; // seconds of a stream playing without a break before a drop is a new trouble
let progressSentAt = 0;
let titleTimer = null;
let rejoinTimer = null; // a live stream waiting to be joined again
let reconnecting = { key: '', count: 0 };

/** Tell the app where listening to an episode has got to: it keeps the place, and counts the episode as heard near the end. */
function sendProgress(item, position, duration, { leaving = false } = {}) {
  if (!isEpisode(item) || !Number.isFinite(position) || position < 0) return;
  progressSentAt = Date.now();
  const body = { position: Math.round(position * 10) / 10 };
  if (Number.isFinite(duration) && duration > 0) body.duration = Math.round(duration * 10) / 10;
  if (leaving) {
    // The page is closing: a request that outlives it.
    fetch(`/api/podcasts/episodes/${item.id}/progress`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), keepalive: true }).catch(() => {});
    return;
  }
  updateItem('episode', item.id, { position: body.position });
  api(`/podcasts/episodes/${item.id}/progress`, { method: 'POST', body })
    .then((episode) => episode && updateItem('episode', item.id, { position: episode.position, played: episode.played }))
    .catch(() => {});
}

/** About to move on from what is playing: keep where an episode got to, stop asking what a station is playing, and stop waiting to rejoin it. */
function leave() {
  const item = active.track;
  if (isEpisode(item) && active.el.currentTime > 0) sendProgress(item, active.el.currentTime, active.el.duration);
  clearInterval(titleTimer);
  titleTimer = null;
  clearTimeout(rejoinTimer);
  rejoinTimer = null;
}

/** Ask now and then what the station is playing (the title its stream carries), for the player bar, the tray and the media keys' overlay. */
function watchStation(item) {
  clearInterval(titleTimer);
  const ask = () =>
    api(`/radio/stations/${item.id}/now`).then(
      (now) => {
        const title = now?.title || '';
        if (!sameItem(current(), item) || title === player.get().liveTitle) return;
        player.set({ liveTitle: title });
        updateMediaSession(item, title);
      },
      () => {},
    );
  titleTimer = setInterval(ask, TITLE_EVERY);
  setTimeout(ask, 3000);
}

/**
 * A live stream that has dropped (or a station that stopped answering): join it again, a few times, a little later each time. The browser
 * says so in one of two ways, by ending the stream (after a pause event) or by failing it (with no pause event at all), so a stream that has
 * dropped is marked as not playing here rather than waited for.
 */
function rejoin(item) {
  if (reconnecting.key !== itemKey(item)) reconnecting = { key: itemKey(item), count: 0 };
  clearTimeout(rejoinTimer);
  rejoinTimer = null;
  if (reconnecting.count >= MAX_RECONNECTS) {
    player.set({ playing: false, loading: false, error: `"${item.name}" stopped playing, and could not be joined again.` });
    return;
  }
  reconnecting.count += 1;
  player.set({ playing: false, loading: true });
  rejoinTimer = setTimeout(() => {
    rejoinTimer = null;
    if (sameItem(current(), item)) loadCurrent(true, { rejoining: true }); // anything else the person has done since has cleared this timer (see leave)
  }, 1500 * reconnecting.count);
}

/** Load the current queue position onto the playing deck (or, if the song is already waiting on the spare, switch to it). */
async function loadCurrent(autoplay, { rejoining = false } = {}) {
  endTransition();
  leave();
  const item = current();
  if (!item) {
    for (const deck of decks) resetDeck(deck);
    startListen(null);
    player.set({ playing: false, loading: false, position: 0, duration: 0, liveTitle: '' });
    applySpeed();
    return;
  }
  startListen(item);
  player.set({ loading: true, error: null, position: 0, duration: item.duration || 0, ...(rejoining ? {} : { liveTitle: '' }) });
  const waiting = spare();
  if (waiting.track && sameItem(waiting.track, item) && loaded(waiting)) {
    // Loaded behind the scenes already: switching to it starts the song at once.
    resetDeck(active);
    active = waiting;
    setFade(waiting, 1);
    player.set({ loading: false, duration: waiting.el.duration });
  } else {
    loadInto(active, item);
  }
  applySpeed();
  updateMediaSession(item);
  if (isStation(item)) {
    watchStation(item);
    if (autoplay && !rejoining) api(`/radio/stations/${item.id}/played`, { method: 'POST' }).catch(() => {});
  }
  if (autoplay) await play();
}

function onFailure(message) {
  const item = current();
  consecutiveErrors += 1;
  const what = item ? `"${isStation(item) ? item.name : item.title}"` : 'this file';
  if (isStation(item)) {
    // The media element can only say "unsupported"; the app can say why (a dead address, HLS, the station's own refusal).
    player.set({ loading: false, playing: false });
    api(`/radio/stations/${item.id}/check`, { method: 'POST' })
      .then((check) => (check.ok ? message : check.message), () => message)
      .then((reason) => sameItem(current(), item) && player.set({ error: `Can't play ${what}: ${reason}` }));
  } else {
    player.set({ loading: false, playing: false, error: `Can't play ${what}: ${message}` });
  }
  const { order, pos } = player.get();
  if (consecutiveErrors < 5 && pos + 1 < order.length) setTimeout(() => advance(false), 700);
}

const failureText = (error) =>
  ({ 1: 'playback was aborted', 2: 'a network error occurred', 3: 'the file could not be decoded', 4: 'the file is missing or its format is not supported' })[
    error?.code
  ] || 'unknown error';

// -- joining one song to the next ----------------------------------------

/** Make sure the spare deck holds the song that follows, so it can start without waiting to load. (Only a song joins a song: a station
 *  is live and would only be listened to later, and a podcast is not joined to the music before it.) */
function maybePreload(s = player.get()) {
  if (transition) return;
  const deck = spare();
  const now = current(s);
  const ahead = now && isTrack(now) && s.repeat !== 'one' && (s.gapless || s.crossfade > 0) ? itemAt(s, planNext(s, { preview: true })) : null;
  const upcoming = ahead && isTrack(ahead) ? ahead : null;
  if (!upcoming) {
    if (deck.track) resetDeck(deck);
    return;
  }
  if (!deck.track || !sameItem(deck.track, upcoming)) loadInto(deck, upcoming);
}

/** Begin blending the playing song into the next one (already loaded on the spare). False if that is not possible. */
function startTransition(blend) {
  const s = player.get();
  const plan = planNext(s, { preview: true });
  const upcoming = itemAt(s, plan);
  const to = spare();
  const from = active;
  if (!upcoming || !to.track || !sameItem(to.track, upcoming) || !loaded(to)) return false;
  const start = graph.ctx.currentTime + 0.005;
  from.nodes.fade.gain.cancelScheduledValues(0);
  from.nodes.fade.gain.value = 1;
  from.nodes.fade.gain.setValueCurveAtTime(FADE_OUT, start, blend);
  to.nodes.fade.gain.cancelScheduledValues(0);
  to.nodes.fade.gain.value = 0;
  to.nodes.fade.gain.setValueCurveAtTime(FADE_IN, start, blend);
  const before = { pos: s.pos, order: s.order, track: current(s) };
  transition = { from, to, endsAt: start + blend + 0.1, before };
  active = to;
  counts.blends += 1;
  consecutiveErrors = 0;
  startListen(upcoming);
  player.set({
    pos: plan.pos,
    ...(plan.order ? { order: plan.order } : {}),
    position: 0,
    duration: to.el.duration,
    loading: false,
    playing: true,
    error: null,
  });
  updateMediaSession(upcoming);
  to.el.play().catch(() => abortTransition());
  return true;
}

/** The next song would not start: stay on the one that was playing. */
function abortTransition() {
  if (!transition) return;
  const { from, to, before } = transition;
  transition = null;
  active = from;
  setFade(from, 1);
  setFade(to, 1);
  to.el.pause();
  to.failed = true; // stays on the spare, so the same song is not tried again and again; the hard path handles it
  player.set({ pos: before.pos, order: before.order, position: from.el.currentTime });
  if (before.track) startListen(before.track);
  updateMediaSession(before.track);
}

/** Finish a blend now (the person paused, sought or skipped in the middle of it): the old song stops, the new one is at full. */
function endTransition() {
  if (!transition) return;
  const { from, to } = transition;
  transition = null;
  resetDeck(from);
  setFade(to, 1);
}

/**
 * The clock for all of it. While something plays, wake just before the playing song's last moments, start the next
 * song then, and otherwise look in every so often: get the next song ready, and finish a blend that has run its course.
 */
function tick() {
  tickTimer = null;
  const el = active.el;
  if (el.paused || !graph) return; // the 'playing' event starts the clock again
  if (transition && graph.ctx.currentTime >= transition.endsAt) endTransition();
  let wait = isTrack(current()) ? 150 : 1000; // a station or an episode is never joined to what follows: nothing to watch for closely
  if (!transition) {
    const s = player.get();
    if (el.currentTime >= 2) maybePreload(s);
    const waiting = spare();
    if (Number.isFinite(el.duration) && el.duration > 0 && loaded(waiting)) {
      const blend = blendSeconds(s, el.duration, waiting.el.duration);
      const remaining = el.duration - el.currentTime;
      if (blend > 0 && remaining <= blend) startTransition(Math.max(GAPLESS_BLEND, remaining));
      else if (blend > 0) wait = Math.max(5, Math.min(150, (remaining - blend) * 1000 - 15));
    }
  }
  tickTimer = setTimeout(tick, wait);
}

function armTick() {
  if (tickTimer === null) tickTimer = setTimeout(tick, 0);
}

function stopTick() {
  clearTimeout(tickTimer);
  tickTimer = null;
}

// -- queue operations ---------------------------------------------------

export function playQueue(tracks, startIndex = 0, { shuffle, source = '' } = {}) {
  if (!tracks.length) return;
  const wantShuffle = shuffle ?? player.get().shuffle;
  const first = Math.min(Math.max(startIndex, 0), tracks.length - 1);
  const order = wantShuffle ? shuffledOrder(tracks.length, first) : tracks.map((_, i) => i);
  consecutiveErrors = 0;
  player.set({ items: tracks.slice(), order, pos: wantShuffle ? 0 : first, shuffle: wantShuffle, source });
  return loadCurrent(true);
}

export function addToQueue(tracks, { next = false } = {}) {
  if (!tracks.length) return;
  const s = player.get();
  if (!s.items.length) {
    player.set({ items: tracks.slice(), order: tracks.map((_, i) => i), pos: 0 });
    return loadCurrent(false);
  }
  const base = s.items.length;
  const added = tracks.map((_, i) => base + i);
  const order = s.order.slice();
  if (next) order.splice(s.pos + 1, 0, ...added);
  else order.push(...added);
  player.set({ items: [...s.items, ...tracks], order });
}

export function removeFromQueue(orderPos) {
  const s = player.get();
  if (orderPos < 0 || orderPos >= s.order.length) return;
  const removedIndex = s.order[orderPos];
  const items = s.items.filter((_, i) => i !== removedIndex);
  const order = s.order.filter((_, p) => p !== orderPos).map((i) => (i > removedIndex ? i - 1 : i));
  if (orderPos === s.pos) {
    const pos = Math.min(s.pos, order.length - 1);
    player.set({ items, order, pos });
    return loadCurrent(s.playing);
  }
  player.set({ items, order, pos: orderPos < s.pos ? s.pos - 1 : s.pos });
}

export function moveInQueue(from, to) {
  const s = player.get();
  if (from === to || from < 0 || to < 0 || from >= s.order.length || to >= s.order.length) return;
  const order = s.order.slice();
  const [moved] = order.splice(from, 1);
  order.splice(to, 0, moved);
  let pos = s.pos;
  if (from === s.pos) pos = to;
  else if (from < s.pos && to >= s.pos) pos -= 1;
  else if (from > s.pos && to <= s.pos) pos += 1;
  player.set({ order, pos });
}

export function jumpTo(orderPos) {
  const s = player.get();
  if (orderPos < 0 || orderPos >= s.order.length) return;
  consecutiveErrors = 0;
  player.set({ pos: orderPos });
  return loadCurrent(true);
}

export function clearQueue() {
  player.set({ items: [], order: [], pos: -1, source: '' });
  return loadCurrent(false);
}

// -- transport ----------------------------------------------------------

export function toggle() {
  const item = current();
  if (!item) return;
  // A live stream is joined afresh whenever it is started: what was paused (or dropped) is only old sound, and an element that failed
  // does not say that it is not playing. Anything else carries on from where it was.
  if (isStation(item)) return player.get().playing ? pause() : loadCurrent(true, { rejoining: true });
  return active.el.paused ? play() : pause();
}

export function pause() {
  endTransition();
  clearTimeout(rejoinTimer); // paused while a dropped stream was about to be joined again: leave it be
  rejoinTimer = null;
  active.el.pause();
  if (isStation(current())) {
    // Live sound that was paused would only be old sound later: let go of the stream, and join it afresh on Play.
    active.el.removeAttribute('src');
    active.el.load();
    player.set({ playing: false, loading: false });
  }
}

function advance(fromEnd) {
  const s = player.get();
  if (s.repeat === 'one' && fromEnd && !isStation(current(s))) {
    active.el.currentTime = 0;
    startListen(current(s));
    return play();
  }
  const plan = planNext(s);
  if (plan) {
    player.set({ pos: plan.pos, ...(plan.order ? { order: plan.order } : {}) });
    return loadCurrent(true);
  }
  if (fromEnd) {
    active.el.pause();
    active.el.currentTime = 0;
    player.set({ playing: false, position: 0 });
  }
}

export function next() {
  consecutiveErrors = 0;
  return advance(false);
}

export function previous() {
  const s = player.get();
  const song = isTrack(current(s)); // Previous restarts a song that has played a while; a station or an episode goes to the one before, or stays
  if (song && active.el.currentTime > 3) {
    seek(0);
    return;
  }
  if (s.pos > 0) {
    player.set({ pos: s.pos - 1 });
    return loadCurrent(true);
  }
  if (s.repeat === 'all' && s.order.length > 1) {
    player.set({ pos: s.order.length - 1 });
    return loadCurrent(true);
  }
  if (song) seek(0);
}

export function seek(seconds) {
  if (isStation(current())) return; // live: there is nothing behind and nothing ahead
  endTransition();
  const el = active.el;
  const duration = el.duration || player.get().duration || 0;
  el.currentTime = Math.min(Math.max(seconds, 0), duration || seconds);
  player.set({ position: el.currentTime });
}

export const seekBy = (delta) => seek(active.el.currentTime + delta);

/** How a podcast episode is skipped through: back a little, forward a little more (skipping an advertisement is the usual reason). */
export const SKIP_BACK = 15;
export const SKIP_FORWARD = 30;

/** How fast podcast episodes play (songs and stations always play at their own speed). */
export function setSpeed(speed) {
  player.set({ speed: SPEEDS.includes(speed) ? speed : 1 });
  applySpeed();
}

function applySpeed() {
  const rate = isEpisode(current()) ? player.get().speed : 1;
  for (const deck of decks) {
    deck.el.defaultPlaybackRate = rate; // loading a new address puts the rate back to this
    deck.el.playbackRate = rate;
  }
}

export function setVolume(volume) {
  player.set({ volume: Math.min(Math.max(volume, 0), 1), muted: false });
  applyVolume();
}

export function toggleMute() {
  player.set({ muted: !player.get().muted });
  applyVolume();
}

export function toggleShuffle() {
  const s = player.get();
  if (!s.items.length) return player.set({ shuffle: !s.shuffle });
  const currentIndex = s.order[s.pos];
  if (!s.shuffle) player.set({ shuffle: true, order: shuffledOrder(s.items.length, currentIndex), pos: 0 });
  else player.set({ shuffle: false, order: s.items.map((_, i) => i), pos: currentIndex });
}

export function cycleRepeat() {
  const repeat = { off: 'all', all: 'one', one: 'off' }[player.get().repeat];
  player.set({ repeat });
}

export function setNormalize(on) {
  player.set({ normalize: on });
  for (const deck of decks) syncGain(deck, true);
}

/** Seconds of crossfade between songs (0 = none). */
export function setCrossfade(seconds) {
  player.set({ crossfade: Math.min(Math.max(Math.round(Number(seconds)) || 0, 0), MAX_CROSSFADE) });
  maybePreload();
}

/** Start the next song as the last one ends, with no silence between them. */
export function setGapless(on) {
  player.set({ gapless: Boolean(on) });
  maybePreload();
}

export function setEq(patch) {
  const eq = { ...player.get().eq, ...patch };
  if (patch.preset && EQ_PRESETS[patch.preset]) eq.gains = [...EQ_PRESETS[patch.preset]];
  player.set({ eq });
  applyEq();
}

/** Keep the queue's copies of a song, station or episode in step with changes made elsewhere (rating, favorite, where listening stopped...). */
export function updateItem(kind, id, patch) {
  player.set((s) => ({ ...s, items: s.items.map((t) => (kindOf(t) === kind && t.id === id ? { ...t, ...patch } : t)) }));
}

export const updateTrack = (trackId, patch) => updateItem('track', trackId, patch);

// ------------------------------------------------------------ media element events

let lastMediaPosition = 0;

for (const deck of decks) {
  const { el } = deck;
  // Only the deck holding the current song speaks for the player; the other is getting ready or fading out.
  const mine = (handler) => (event) => {
    if (active === deck) handler(event);
  };

  el.addEventListener(
    'playing',
    mine(() => {
      consecutiveErrors = 0;
      player.set({ playing: true, loading: false, error: null });
      if (listen && !listen.announced && listen.id === current()?.id) {
        listen.announced = true; // once per song; the server stays quiet unless "show what I'm playing" is on
        api('/now-playing', { method: 'POST', body: { track_id: listen.id } }).catch(() => {});
      }
      if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing';
      armTick();
    }),
  );
  el.addEventListener(
    'pause',
    mine(() => {
      player.set({ playing: false });
      if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused';
      stopTick();
      const item = current();
      if (isEpisode(item) && !el.ended) sendProgress(item, el.currentTime, el.duration); // paused (or the sleep timer): keep the place
    }),
  );
  el.addEventListener('waiting', mine(() => player.set({ loading: true })));
  el.addEventListener('canplay', mine(() => player.set({ loading: false })));
  el.addEventListener(
    'durationchange',
    mine(() => {
      if (Number.isFinite(el.duration)) player.set({ duration: el.duration });
    }),
  );
  el.addEventListener('error', () => {
    if (active !== deck) {
      deck.failed = true; // a song that will not load ahead of time is dealt with when its turn comes
      return;
    }
    const item = current();
    if (isStation(item) && el.currentTime > 0) rejoin(item); // it was playing and has dropped
    else onFailure(failureText(el.error));
  });
  el.addEventListener(
    'ended',
    mine(() => {
      counts.hardEnds += 1;
      const item = current();
      if (isStation(item)) return rejoin(item); // a live stream does not end: it has dropped
      if (isEpisode(item)) sendProgress(item, el.duration, el.duration); // to the end: heard
      return advance(true);
    }),
  );
  el.addEventListener(
    'timeupdate',
    mine(() => {
      const time = el.currentTime;
      player.set({ position: time });

      const item = current();
      if (isEpisode(item) && !el.paused && Date.now() - progressSentAt >= PROGRESS_EVERY) sendProgress(item, time, el.duration);
      if (isStation(item) && time > STEADY_AFTER) reconnecting.count = 0; // it has been playing steadily: the next drop is a new trouble

      if (listen && listen.id === current()?.id) {
        const delta = time - listen.lastTime;
        if (delta > 0 && delta < 1.5 && !el.paused) listen.ms += delta * 1000;
        listen.lastTime = time;
        const duration = el.duration || listen.duration || 0;
        const needed = Math.max(10, Math.min(duration * 0.5, 240)) * 1000;
        if (!listen.logged && listen.ms >= needed) {
          listen.logged = true;
          api('/plays', { method: 'POST', body: { track_id: listen.id, ms_played: Math.round(listen.ms), started_at: listen.startedAt } })
            .then(() => listeners['play-logged'].forEach((fn) => fn(listen?.id)))
            .catch(() => {});
        }
      }

      const { sleepAt } = player.get();
      if (sleepAt && Date.now() >= sleepAt) {
        player.set({ sleepAt: null });
        pause();
      }

      if ('mediaSession' in navigator && Math.abs(time - lastMediaPosition) >= 1 && Number.isFinite(el.duration)) {
        lastMediaPosition = time;
        try {
          navigator.mediaSession.setPositionState({ duration: el.duration, position: time, playbackRate: el.playbackRate });
        } catch {
          /* position state is best-effort */
        }
      }
    }),
  );
}

// The page is closing: an episode keeps its place with a request that outlives the page.
window.addEventListener('pagehide', () => {
  const item = active.track;
  if (isEpisode(item) && active.el.currentTime > 0) sendProgress(item, active.el.currentTime, active.el.duration, { leaving: true });
});

export function setSleepTimer(minutes) {
  player.set({ sleepAt: minutes ? Date.now() + minutes * 60000 : null });
}

// ------------------------------------------------------------ OS media controls

function updateMediaSession(item, liveTitle = '') {
  if (!item || !('mediaSession' in navigator) || typeof MediaMetadata === 'undefined') return;
  const shown = describe(item, liveTitle);
  const picture = artSrc(item, 512);
  navigator.mediaSession.metadata = new MediaMetadata({
    title: shown.title,
    artist: shown.subtitle,
    album: isTrack(item) ? item.album : '',
    artwork: picture ? [{ src: picture, sizes: '512x512', type: 'image/jpeg' }] : [],
  });
}

if ('mediaSession' in navigator) {
  const handlers = {
    play: () => toggle(),
    pause: () => pause(),
    previoustrack: () => previous(),
    nexttrack: () => next(),
    seekbackward: () => seekBy(isEpisode(current()) ? -SKIP_BACK : -10),
    seekforward: () => seekBy(isEpisode(current()) ? SKIP_FORWARD : 10),
    seekto: (d) => seek(d.seekTime),
    stop: () => pause(),
  };
  for (const [action, handler] of Object.entries(handlers)) {
    try {
      navigator.mediaSession.setActionHandler(action, handler);
    } catch {
      /* not every action exists everywhere */
    }
  }
}
