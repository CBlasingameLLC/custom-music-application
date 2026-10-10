// The player: one <audio> element routed through a Web Audio graph, plus the queue.
//
//   <audio> -> ReplayGain -> 10-band EQ -> master volume -> analyser -> speakers
//
// The queue is `items` (what was queued) plus `order` (indices into items, in play
// order) and `pos` (where we are in `order`). Shuffle only rewrites `order`.
import { api, createStore } from './lib.js';

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
  sleepAt: null,
  source: '',
});

export const current = (state = player.get()) => state.items[state.order[state.pos]] || null;

const audio = new Audio();
audio.preload = 'auto';
let graph = null;
let listen = null; // what has been listened to for the current track, for play logging
let consecutiveErrors = 0;
let replayGainFor = null;
const listeners = { 'play-logged': new Set() };

export const onPlayLogged = (fn) => {
  listeners['play-logged'].add(fn);
  return () => listeners['play-logged'].delete(fn);
};

// ------------------------------------------------------------ audio graph

function ensureGraph() {
  if (graph) return graph;
  const Context = window.AudioContext || window.webkitAudioContext;
  const ctx = new Context();
  const source = ctx.createMediaElementSource(audio);
  const rg = ctx.createGain();
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
  let node = source;
  for (const next of [rg, ...filters, master, analyser]) {
    node.connect(next);
    node = next;
  }
  analyser.connect(ctx.destination);
  graph = { ctx, rg, filters, master, analyser };
  applyVolume();
  applyEq();
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

async function applyReplayGain(track) {
  replayGainFor = track.id;
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
  if (replayGainFor === track.id && graph) graph.rg.gain.value = gain;
}

// ------------------------------------------------------------ persistence

let persistTimer = null;
function persist() {
  clearTimeout(persistTimer);
  persistTimer = setTimeout(() => {
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
        }),
      );
    } catch {
      /* storage may be unavailable */
    }
  }, 400);
}
player.subscribe(persist);

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
    audio.src = `/api/tracks/${kept[order[pos]].id}/stream`;
    const resumeAt = saved.position || 0;
    audio.addEventListener('loadedmetadata', () => (audio.currentTime = resumeAt), { once: true });
    player.set({ position: resumeAt, duration: kept[order[pos]].duration || 0 });
    updateMediaSession(kept[order[pos]]);
  } catch {
    /* an empty queue is a fine fallback */
  }
}

// ------------------------------------------------------------ playback

async function play() {
  ensureGraph();
  const track = current();
  if (track && replayGainFor !== track.id) applyReplayGain(track);
  try {
    await graph.ctx.resume();
    await audio.play();
  } catch (error) {
    if (error.name !== 'AbortError') onFailure(error.message || String(error));
  }
}

async function loadCurrent(autoplay) {
  const track = current();
  if (!track) {
    audio.pause();
    audio.removeAttribute('src');
    audio.load();
    player.set({ playing: false, loading: false, position: 0, duration: 0 });
    return;
  }
  listen = { id: track.id, startedAt: Date.now() / 1000, ms: 0, logged: false, announced: false, lastTime: 0, duration: track.duration };
  player.set({ loading: true, error: null, position: 0, duration: track.duration || 0 });
  audio.src = `/api/tracks/${track.id}/stream`;
  updateMediaSession(track);
  replayGainFor = null; // play() applies it once the audio graph exists
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

function shuffled(count, firstIndex) {
  const rest = [];
  for (let i = 0; i < count; i++) if (i !== firstIndex) rest.push(i);
  for (let i = rest.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [rest[i], rest[j]] = [rest[j], rest[i]];
  }
  return firstIndex === undefined || firstIndex < 0 ? rest : [firstIndex, ...rest];
}

// -- queue operations ---------------------------------------------------

export function playQueue(tracks, startIndex = 0, { shuffle, source = '' } = {}) {
  if (!tracks.length) return;
  const wantShuffle = shuffle ?? player.get().shuffle;
  const first = Math.min(Math.max(startIndex, 0), tracks.length - 1);
  const order = wantShuffle ? shuffled(tracks.length, first) : tracks.map((_, i) => i);
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
  if (audio.paused) return play();
  audio.pause();
}

export function pause() {
  audio.pause();
}

function advance(fromEnd) {
  const s = player.get();
  if (s.repeat === 'one' && fromEnd) {
    audio.currentTime = 0;
    listen = { ...listen, ms: 0, logged: false, announced: false, startedAt: Date.now() / 1000, lastTime: 0 };
    return play();
  }
  if (s.pos + 1 < s.order.length) {
    player.set({ pos: s.pos + 1 });
    return loadCurrent(true);
  }
  if (s.repeat === 'all' && s.order.length) {
    const order = s.shuffle ? shuffled(s.items.length) : s.items.map((_, i) => i);
    player.set({ order, pos: 0 });
    return loadCurrent(true);
  }
  if (fromEnd) {
    audio.pause();
    audio.currentTime = 0;
    player.set({ playing: false, position: 0 });
  }
}

export function next() {
  consecutiveErrors = 0;
  return advance(false);
}

export function previous() {
  const s = player.get();
  if (audio.currentTime > 3) {
    audio.currentTime = 0;
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
  audio.currentTime = 0;
}

export function seek(seconds) {
  const duration = audio.duration || player.get().duration || 0;
  audio.currentTime = Math.min(Math.max(seconds, 0), duration || seconds);
  player.set({ position: audio.currentTime });
}

export const seekBy = (delta) => seek(audio.currentTime + delta);

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
  if (!s.shuffle) player.set({ shuffle: true, order: shuffled(s.items.length, currentIndex), pos: 0 });
  else player.set({ shuffle: false, order: s.items.map((_, i) => i), pos: currentIndex });
}

export function cycleRepeat() {
  const repeat = { off: 'all', all: 'one', one: 'off' }[player.get().repeat];
  player.set({ repeat });
}

export function setNormalize(on) {
  player.set({ normalize: on });
  const track = current();
  if (track) applyReplayGain(track);
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

audio.addEventListener('playing', () => {
  consecutiveErrors = 0;
  player.set({ playing: true, loading: false, error: null });
  if (listen && !listen.announced && listen.id === current()?.id) {
    listen.announced = true; // once per song; the server stays quiet unless "show what I'm playing" is on
    api('/now-playing', { method: 'POST', body: { track_id: listen.id } }).catch(() => {});
  }
  if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing';
});
audio.addEventListener('pause', () => {
  player.set({ playing: false });
  if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused';
});
audio.addEventListener('waiting', () => player.set({ loading: true }));
audio.addEventListener('canplay', () => player.set({ loading: false }));
audio.addEventListener('durationchange', () => {
  if (Number.isFinite(audio.duration)) player.set({ duration: audio.duration });
});
audio.addEventListener('error', () => onFailure(failureText(audio.error)));
audio.addEventListener('ended', () => advance(true));

let lastMediaPosition = 0;
audio.addEventListener('timeupdate', () => {
  const time = audio.currentTime;
  player.set({ position: time });

  if (listen && listen.id === current()?.id) {
    const delta = time - listen.lastTime;
    if (delta > 0 && delta < 1.5 && !audio.paused) listen.ms += delta * 1000;
    listen.lastTime = time;
    const duration = audio.duration || listen.duration || 0;
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
    audio.pause();
  }

  if ('mediaSession' in navigator && Math.abs(time - lastMediaPosition) >= 1 && Number.isFinite(audio.duration)) {
    lastMediaPosition = time;
    try {
      navigator.mediaSession.setPositionState({ duration: audio.duration, position: time, playbackRate: audio.playbackRate });
    } catch {
      /* position state is best-effort */
    }
  }
});

export function setSleepTimer(minutes) {
  player.set({ sleepAt: minutes ? Date.now() + minutes * 60000 : null });
}

// ------------------------------------------------------------ OS media controls

function updateMediaSession(track) {
  if (!('mediaSession' in navigator) || typeof MediaMetadata === 'undefined') return;
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
