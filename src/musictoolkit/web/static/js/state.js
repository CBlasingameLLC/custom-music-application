// App-wide UI state: routing, toasts, background jobs, dialogs, context menu.
import { api, createStore } from './lib.js';
import { accentVars, normalizeAccent } from './appearance.js';

// ------------------------------------------------------------ routing (hash based)

function parseRoute() {
  const raw = location.hash.slice(1) || '/';
  const [path, query = ''] = raw.split('?');
  return {
    path,
    parts: path.split('/').filter(Boolean).map(decodeURIComponent),
    query: Object.fromEntries(new URLSearchParams(query)),
  };
}

export const route = createStore(parseRoute());
window.addEventListener('hashchange', () => route.set(parseRoute()));
export const go = (path) => {
  location.hash = '#' + path;
};
export const href = (path) => '#' + path;
export const enc = encodeURIComponent;

// ------------------------------------------------------------ toasts

export const toasts = createStore({ items: [] });
let toastCounter = 0;

export function dismissToast(id) {
  toasts.set((s) => ({ items: s.items.filter((t) => t.id !== id) }));
}

/** `action` is an optional { label, onClick } shown as a button in the toast (e.g. Undo). */
export function toast(text, kind = 'info', ms = 4500, action = null) {
  const id = ++toastCounter;
  toasts.set((s) => ({ items: [...s.items.slice(-3), { id, text, kind, action }] }));
  if (ms) setTimeout(() => dismissToast(id), ms);
  return id;
}

export const notifyError = (error) => toast(error?.message || String(error), 'error', 8000);

// ------------------------------------------------------------ UI preferences

const prefs = (() => {
  try {
    return JSON.parse(localStorage.getItem('mtk.ui.v1') || '{}');
  } catch {
    return {};
  }
})();

export const ui = createStore({
  theme: prefs.theme || 'dark',
  accent: normalizeAccent(prefs.accent), // a preset's id or a #rrggbb colour (js/appearance.js)
  density: prefs.density === 'compact' ? 'compact' : 'comfortable',
  queueOpen: false,
  jobsOpen: false,
  songColumns: prefs.songColumns || null,
});

// How the app looks: the theme, the accent colour (and the colours derived from it) and the density go on the page, and are
// remembered together with the derived colours, which js/boot.js puts back before the next start is drawn.
function applyLook(s) {
  const root = document.documentElement;
  root.dataset.theme = s.theme;
  root.dataset.density = s.density;
  const vars = accentVars(s.accent, s.theme);
  for (const [name, value] of Object.entries(vars)) root.style.setProperty(name, value);
  try {
    localStorage.setItem('mtk.ui.v1', JSON.stringify({ theme: s.theme, songColumns: s.songColumns, accent: s.accent, density: s.density, vars }));
  } catch {
    /* storage unavailable */
  }
}
ui.subscribe(applyLook);
applyLook(ui.get());
export const setTheme = (theme) => ui.set({ theme });
export const setAccent = (accent) => ui.set({ accent: normalizeAccent(accent) });
export const setDensity = (density) => ui.set({ density: density === 'compact' ? 'compact' : 'comfortable' });

// ------------------------------------------------------------ shared library data

export const library = createStore({ rev: 0, facets: null, playlists: [], settings: null, about: null });

/** Call after anything that changes what the library lists (scan finished, tags edited, ...). */
export const bumpLibrary = () => library.set((s) => ({ ...s, rev: s.rev + 1 }));

export async function loadPlaylists() {
  const { items } = await api('/playlists');
  library.set((s) => ({ ...s, playlists: items }));
  return items;
}
export async function loadFacets() {
  const facets = await api('/facets');
  library.set((s) => ({ ...s, facets }));
  return facets;
}
export async function loadSettings() {
  const settings = await api('/settings');
  library.set((s) => ({ ...s, settings }));
  return settings;
}
export async function loadAbout() {
  const about = await api('/about');
  library.set((s) => ({ ...s, about }));
  return about;
}

// ------------------------------------------------------------ background jobs

export const jobs = createStore({ items: [] });
const FINISHED = ['done', 'error', 'cancelled'];
const loadedAt = Date.now() / 1000 - 2; // jobs that finished before this page opened are old news
const handled = new Set();
let pollTimer = null;
const jobFinishedHandlers = new Set();
export const onJobFinished = (fn) => {
  jobFinishedHandlers.add(fn);
  return () => jobFinishedHandlers.delete(fn);
};

export async function refreshJobs() {
  try {
    const { jobs: items } = await api('/jobs');
    // Notify for every job that finished since the page opened - including ones that started and
    // finished between two polls, which a "was running last time" check would miss.
    for (const job of items) {
      if (FINISHED.includes(job.status) && !handled.has(job.id)) {
        handled.add(job.id);
        if ((job.finished_at || 0) >= loadedAt) jobFinishedHandlers.forEach((fn) => fn(job));
      }
    }
    jobs.set({ items });
    return items;
  } catch {
    return jobs.get().items;
  }
}

export function startJobPolling() {
  if (pollTimer) return;
  const tick = async () => {
    const items = await refreshJobs();
    const busy = items.some((j) => j.status === 'queued' || j.status === 'running');
    pollTimer = setTimeout(tick, busy ? 700 : 4000);
  };
  tick();
}

/** Show a job right away instead of waiting for the next poll. */
export function trackJob(job) {
  jobs.set((s) => ({ items: [job, ...s.items.filter((j) => j.id !== job.id)] }));
  clearTimeout(pollTimer);
  pollTimer = null;
  startJobPolling();
}

onJobFinished((job) => {
  if (job.status === 'error') toast(`${job.title} failed: ${job.error}`, 'error', 10000);
  else if (job.status === 'done' && job.kind === 'scan') {
    const r = job.result || {};
    toast(`Library updated: ${r.added || 0} added, ${r.updated || 0} changed, ${r.missing || 0} missing`, 'success');
  } else if (job.status === 'done' && job.kind === 'sync') {
    const r = job.result || {};
    const note = r.errors_total ? ` ${r.errors_total} could not be copied.` : '';
    toast(`Copied ${r.copied || 0} ${r.copied === 1 ? 'song' : 'songs'} to ${r.label || 'the device'}.${note}`, r.errors_total || r.aborted ? 'info' : 'success', 8000);
  } else if (job.status === 'done' && job.kind === 'rip') {
    const r = job.result || {};
    const note = r.failed ? ` ${r.failed} could not be read.` : '';
    toast(`Ripped ${r.ripped || 0} ${r.ripped === 1 ? 'song' : 'songs'}.${note}`, r.failed ? 'info' : 'success', 9000);
  } else if (job.status === 'cancelled' && job.kind === 'rip') {
    toast('Stopped. Songs already ripped are in your library, and the history lets you take them back.', 'info', 9000);
  } else if (job.status === 'done' && job.kind === 'burn') {
    const r = job.result || {};
    const what = r.files !== undefined ? `${r.files} ${r.files === 1 ? 'file' : 'files'}` : `${r.songs || 0} ${r.songs === 1 ? 'song' : 'songs'}`;
    toast(`Burned ${what}.${r.verified ? ' Every file was read back from the disc and matches.' : ''}${r.ejected ? ' The disc is out.' : ''}`, 'success', 9000);
  } else if (job.status === 'cancelled' && job.kind === 'burn') {
    toast('Stopped. The History says what became of the disc.', 'info', 9000);
  } else if (job.status === 'done' && job.kind === 'iso') {
    toast(`Saved the disc image: ${(job.result || {}).output || ''}`, 'success', 9000);
  } else if (job.status === 'done' && job.kind === 'erase') {
    toast((job.result || {}).message || 'The disc was erased.', 'success', 9000);
  } else if (job.status === 'done' && job.kind === 'rip-undo') {
    const r = job.result || {};
    const note = r.changed?.length ? ` ${r.changed.length} changed since, so they were left alone.` : '';
    toast(`Moved ${r.trashed || 0} ${r.trashed === 1 ? 'song' : 'songs'} to the Recycle Bin.${note}`, r.changed?.length || r.failed?.length ? 'info' : 'success', 9000);
  }
  if (job.status === 'done' || job.status === 'cancelled' || (job.status === 'error' && job.kind === 'rip')) { // (a rip that stopped part way has still added songs)
    bumpLibrary();
    loadFacets().catch(() => {});
    loadAbout().catch(() => {});
  }
});

// ------------------------------------------------------------ the list of songs to burn

const BURN_LIST_KEY = 'mtk.burnlist.v1';
export const BURN_LIST_MAX = 500; // what the server takes in one go (a music CD holds 99; a data disc holds as many files as fit)
const burnListBefore = (() => {
  try {
    const saved = JSON.parse(sessionStorage.getItem(BURN_LIST_KEY) || '[]');
    return Array.isArray(saved) ? saved.filter((i) => Number.isInteger(i?.id)).slice(0, BURN_LIST_MAX) : [];
  } catch {
    return [];
  }
})();

/** The songs somebody is putting together for a disc, in the order they go on it: { items: [{ id, title, artist, seconds }] }. Kept for the session. */
export const burnList = createStore({ items: burnListBefore });
burnList.subscribe((s) => {
  try {
    sessionStorage.setItem(BURN_LIST_KEY, JSON.stringify(s.items));
  } catch {
    /* storage unavailable */
  }
});

/** Which tab of the Discs screen to show, asked for from elsewhere (a toast's "Open"); `n` changes every time, so that asking twice works. */
export const discsFocus = createStore({ tab: null, n: 0 });
export function openDiscs(tab) {
  discsFocus.set((s) => ({ tab, n: s.n + 1 }));
  go('/discs');
}

/** Put songs at the end of the list. A song may be on it twice. Tells the person what happened, with a way to the list. */
export function addToBurnList(tracks) {
  const room = BURN_LIST_MAX - burnList.get().items.length;
  const added = tracks.slice(0, Math.max(room, 0)).map((t) => ({ id: t.id, title: t.title || '', artist: t.artist || t.album_artist || '', seconds: t.duration || 0 }));
  if (!added.length) {
    toast(`The burn list is full (${BURN_LIST_MAX} songs).`, 'info');
    return;
  }
  burnList.set((s) => ({ items: [...s.items, ...added] }));
  const left = tracks.length - added.length;
  const words = added.length === 1 ? `Added "${added[0].title}"` : `Added ${added.length} songs`;
  toast(`${words} to the burn list${left > 0 ? ` (${left} did not fit)` : ''}`, 'success', 6000, { label: 'Open the list', onClick: () => openDiscs('burn') });
}
export const removeFromBurnList = (index) => burnList.set((s) => ({ items: s.items.filter((_, i) => i !== index) }));
export const clearBurnList = () => burnList.set({ items: [] });
export function moveInBurnList(index, by) {
  burnList.set((s) => {
    const to = index + by;
    if (to < 0 || to >= s.items.length) return s;
    const items = [...s.items];
    [items[index], items[to]] = [items[to], items[index]];
    return { items };
  });
}

// ------------------------------------------------------------ context menu

export const menu = createStore({ open: false, x: 0, y: 0, items: [], opener: null });

/** The element with the focus, unless that is nothing in particular (the page body) or part of a dialog that has just closed. */
function focusedElement() {
  const element = document.activeElement;
  if (!element || element === document.body) return null;
  const dialog = element.closest?.('[data-modal-id]');
  if (dialog && !modals.get().stack.some((m) => String(m.id) === dialog.dataset.modalId)) return null;
  return element;
}

/** Give the focus back to `element`, unless the person has moved on: it stays wherever they put it. (Nowhere, or in a menu or dialog that is closing, is not "moved on".) */
function restoreFocus(element) {
  const now = document.activeElement;
  const lost = !now || now === document.body || now.closest?.('[data-modal-id], .menu');
  if (element?.isConnected && lost) element.focus({ preventScroll: true });
}

/** `event` may be a real mouse event or just { clientX, clientY } to anchor the menu somewhere. */
export function openMenu(event, items) {
  event.preventDefault?.();
  event.stopPropagation?.();
  menu.set({ open: true, x: event.clientX, y: event.clientY, items, opener: focusedElement() });
}
/** `restore` puts the focus back where it was before the menu opened: for Escape and for a chosen item, not for a click somewhere else. */
export function closeMenu({ restore = false } = {}) {
  const { opener } = menu.get();
  menu.set({ open: false, x: 0, y: 0, items: [], opener: null });
  if (restore) restoreFocus(opener);
}

// ------------------------------------------------------------ dialogs

export const modals = createStore({ stack: [] });
let modalCounter = 0;

let lastClosed = { opener: null, at: 0 };

export function closeModal(id) {
  const entry = modals.get().stack.find((m) => m.id === id);
  if (!entry) return;
  modals.set((s) => ({ stack: s.stack.filter((m) => m.id !== id) }));
  entry.onClose?.();
  // Back to what had the focus before the dialog opened, once the page has caught up with the dialog being gone.
  lastClosed = { opener: entry.opener, at: Date.now() };
  setTimeout(() => restoreFocus(entry.opener), 0);
}

/**
 * Show a dialog. `render(close)` returns the body; `onClose` fires however it was dismissed. Closing it puts the focus back
 * where it was, also when the answer led straight to another dialog (a confirmation after a confirmation).
 */
export function openModal({ title, render, width = 480, onClose }) {
  const id = ++modalCounter;
  const close = () => closeModal(id);
  const opener = focusedElement() || (Date.now() - lastClosed.at < 1000 ? lastClosed.opener : null);
  modals.set((s) => ({ stack: [...s.stack, { id, title, render, width, close, onClose, opener }] }));
  return close;
}

// ------------------------------------------------------------ desktop shell bridge

/** Present only inside the Electron app; the browser build falls back to typed paths. */
export const desktop = window.mtk || null;

// ------------------------------------------------------------ app updates (desktop only)

/** { state: idle | checking | up-to-date | downloading | ready | error | disabled, current, version, percent, error, checkedAt } */
export const updates = createStore({ status: null });

if (desktop?.update) {
  const apply = (status) => {
    if (!status) return;
    const before = updates.get().status;
    updates.set({ status });
    if (status.state === 'ready' && before?.state !== 'ready') {
      toast(`Music Toolkit ${status.version} is ready to install.`, 'info', 0, { label: 'Restart now', onClick: () => desktop.update.install() });
    }
  };
  desktop.update.status().then(apply).catch(() => {});
  desktop.update.onStatus(apply);
}
