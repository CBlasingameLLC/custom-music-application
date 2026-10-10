// App-wide UI state: routing, toasts, background jobs, dialogs, context menu.
import { api, createStore } from './lib.js';

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
  queueOpen: false,
  jobsOpen: false,
  songColumns: prefs.songColumns || null,
});

ui.subscribe((s) => {
  document.documentElement.dataset.theme = s.theme;
  try {
    localStorage.setItem('mtk.ui.v1', JSON.stringify({ theme: s.theme, songColumns: s.songColumns }));
  } catch {
    /* storage unavailable */
  }
});
document.documentElement.dataset.theme = ui.get().theme;
export const setTheme = (theme) => ui.set({ theme });

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
  }
  if (job.status === 'done' || job.status === 'cancelled') {
    bumpLibrary();
    loadFacets().catch(() => {});
    loadAbout().catch(() => {});
  }
});

// ------------------------------------------------------------ context menu

export const menu = createStore({ open: false, x: 0, y: 0, items: [] });

/** `event` may be a real mouse event or just { clientX, clientY } to anchor the menu somewhere. */
export function openMenu(event, items) {
  event.preventDefault?.();
  event.stopPropagation?.();
  menu.set({ open: true, x: event.clientX, y: event.clientY, items });
}
export const closeMenu = () => menu.set({ open: false, x: 0, y: 0, items: [] });

// ------------------------------------------------------------ dialogs

export const modals = createStore({ stack: [] });
let modalCounter = 0;

export function closeModal(id) {
  const entry = modals.get().stack.find((m) => m.id === id);
  if (!entry) return;
  modals.set((s) => ({ stack: s.stack.filter((m) => m.id !== id) }));
  entry.onClose?.();
}

/** Show a dialog. `render(close)` returns the body; `onClose` fires however it was dismissed. */
export function openModal({ title, render, width = 480, onClose }) {
  const id = ++modalCounter;
  const close = () => closeModal(id);
  modals.set((s) => ({ stack: [...s.stack, { id, title, render, width, close, onClose }] }));
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
