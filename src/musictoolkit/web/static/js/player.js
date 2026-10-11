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
import { api, createStore } from './lib.js';
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
const streamUrl = (track) => `/api/tracks/${track.id}/stream`;
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
  ...counts,
  decks: decks.map((d) => ({
    name: d.name,
    track: d.track ? d.track.id : null,
    paused: d.el.paused,
    ended: d.el.ended,
    time: d.el.currentTime,
    duration: d.el.duration,
    ready: d.el.readyState,
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
  if (!track || (!force && deck.gainFor === track.id)) return;
  deck.gainFor = track.id;
  let gain = 1;
  if (player.get().normalize) {
    try {
      const info = await api(`/tracks/${track.id}/info`);
      const db = info.replaygain_track_db ?? info.replaygain_album_db;
      if (typeof db === 'number') gain = Math.min(Math.pow(10, db / 20), 1.5);
    } catch {
      /* no gain info is fine */
    }
  }
  if (deck.track && deck.track.id === track.id) {
    deck.gain = gain;
    applyDeckGain(deck);
  }
}

/** Put a song on a deck (it starts loading at once; it plays when told to). */
function loadInto(deck, track) {
  deck.track = track;
  deck.gain = null;
  deck.gainFor = null;
  deck.failed = false;
  deck.el.src = streamUrl(track);
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
        ids: s.items.map((t) => t.id),
        order: s.order,
        pos: s.pos,
        position: s.position,
        volume: s.volume,
        shuffle: s.shuffle,
        repeat: s.repeat,
        normalize: s.normalize,
        eq: s.eq,
        crossfade: s.crossfade,
        gapless: s.gapless,
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
    const { items } = await api('/tracks/by-ids', { method: 'POST', body: { ids: saved.ids } });
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

function startListen(track) {
  listen = { id: track.id, startedAt: Date.now() / 1000, ms: 0, logged: false, announced: false, lastTime: 0, duration: track.duration };
}

/** Load the current queue position onto the playing deck (or, if the song is already waiting on the spare, switch to it). */
async function loadCurrent(autoplay) {
  endTransition();
  const track = current();
  if (!track) {
    for (const deck of decks) resetDeck(deck);
    player.set({ playing: false, loading: false, position: 0, duration: 0 });
    return;
  }
  startListen(track);
  player.set({ loading: true, error: null, position: 0, duration: track.duration || 0 });
  const waiting = spare();
  if (waiting.track && waiting.track.id === track.id && loaded(waiting)) {
    // Loaded behind the scenes already: switching to it starts the song at once.
    resetDeck(active);
    active = waiting;
    setFade(waiting, 1);
    player.set({ loading: false, duration: waiting.el.duration });
  } else {
    loadInto(active, track);
  }
  updateMediaSession(track);
  if (autoplay) await play();
}

function onFailure(message) {
  const track = current();
  consecutiveErrors += 1;
  player.set({ loading: false, playing: false, error: `Can't play ${track ? `"${track.title}"` : 'this file'}: ${message}` });
  const { order, pos } = player.get();
  if (consecutiveErrors < 5 && pos + 1 < order.length) setTimeout(() => advance(false), 700);
}

const failureText = (error) =>
  ({ 1: 'playback was aborted', 2: 'a network error occurred', 3: 'the file could not be decoded', 4: 'the file is missing or its format is not supported' })[
    error?.code
  ] || 'unknown error';

// -- joining one song to the next ----------------------------------------

/** Make sure the spare deck holds the song that follows, so it can start without waiting to load. */
function maybePreload(s = player.get()) {
  if (transition) return;
  const deck = spare();
  const upcoming = s.repeat !== 'one' && (s.gapless || s.crossfade > 0) ? itemAt(s, planNext(s, { preview: true })) : null;
  if (!upcoming) {
    if (deck.track) resetDeck(deck);
    return;
  }
  if (!deck.track || deck.track.id !== upcoming.id) loadInto(deck, upcoming);
}

/** Begin blending the playing song into the next one (already loaded on the spare). False if that is not possible. */
function startTransition(blend) {
  const s = player.get();
  const plan = planNext(s, { preview: true });
  const upcoming = itemAt(s, plan);
  const to = spare();
  const from = active;
  if (!upcoming || !to.track || to.track.id !== upcoming.id || !loaded(to)) return false;
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
  let wait = 150;
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
  const s = player.get();
  if (!current(s)) return;
  if (active.el.paused) return play();
  pause();
}

export function pause() {
  endTransition();
  active.el.pause();
}

function advance(fromEnd) {
  const s = player.get();
  if (s.repeat === 'one' && fromEnd) {
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
  if (active.el.currentTime > 3) {
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
  seek(0);
}

export function seek(seconds) {
  endTransition();
  const el = active.el;
  const duration = el.duration || player.get().duration || 0;
  el.currentTime = Math.min(Math.max(seconds, 0), duration || seconds);
  player.set({ position: el.currentTime });
}

export const seekBy = (delta) => seek(active.el.currentTime + delta);

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

export function updateTrack(trackId, patch) {
  // Keep the queue's copies of a track in step with edits (rating, favorite, ...).
  player.set((s) => ({ ...s, items: s.items.map((t) => (t.id === trackId ? { ...t, ...patch } : t)) }));
}

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
    if (active === deck) onFailure(failureText(el.error));
    else deck.failed = true; // a song that will not load ahead of time is dealt with when its turn comes
  });
  el.addEventListener(
    'ended',
    mine(() => {
      counts.hardEnds += 1;
      advance(true);
    }),
  );
  el.addEventListener(
    'timeupdate',
    mine(() => {
      const time = el.currentTime;
      player.set({ position: time });

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

export function setSleepTimer(minutes) {
  player.set({ sleepAt: minutes ? Date.now() + minutes * 60000 : null });
}

// ------------------------------------------------------------ OS media controls

function updateMediaSession(track) {
  if (!track || !('mediaSession' in navigator) || typeof MediaMetadata === 'undefined') return;
  navigator.mediaSession.metadata = new MediaMetadata({
    title: track.title,
    artist: track.artist,
    album: track.album,
    artwork: [{ src: `/api/art/track/${track.id}?size=512`, sizes: '512x512', type: 'image/jpeg' }],
  });
}

if ('mediaSession' in navigator) {
  const handlers = {
    play: () => toggle(),
    pause: () => pause(),
    previoustrack: () => previous(),
    nexttrack: () => next(),
    seekbackward: () => seekBy(-10),
    seekforward: () => seekBy(10),
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
