// Shared helpers: templating, hooks, API client, tiny store, formatting.
import { h, render, Fragment } from '../vendor/preact.js';
import { useState, useEffect, useRef, useMemo, useCallback, useLayoutEffect } from '../vendor/hooks.js';
import htm from '../vendor/htm.js';

export const html = htm.bind(h);
export { h, render, Fragment, useState, useEffect, useRef, useMemo, useCallback, useLayoutEffect };

// ---------------------------------------------------------------- API

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export async function api(path, { method = 'GET', body, params } = {}) {
  const url = new URL('/api' + path, location.origin);
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== undefined && value !== null && value !== '') url.searchParams.set(key, value);
  }
  const response = await fetch(url, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const data = await response.json();
      detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
    } catch {
      /* body was not JSON */
    }
    throw new ApiError(response.status, detail);
  }
  return response.status === 204 ? null : response.json();
}

// ---------------------------------------------------------------- store

export function createStore(initial) {
  let state = initial;
  const subscribers = new Set();
  return {
    get: () => state,
    set(patch) {
      state = typeof patch === 'function' ? patch(state) : { ...state, ...patch };
      subscribers.forEach((fn) => fn(state));
    },
    subscribe(fn) {
      subscribers.add(fn);
      return () => subscribers.delete(fn);
    },
  };
}

export function useStore(store, selector = (s) => s) {
  const [value, setValue] = useState(() => selector(store.get()));
  useEffect(() => {
    const update = () => setValue(selector(store.get()));
    update();
    return store.subscribe(update);
  }, [store]);
  return value;
}

// Run an async loader whenever `deps` change; returns { data, error, loading, reload }.
export function useAsync(loader, deps) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setState((s) => ({ ...s, loading: true, error: null }));
    Promise.resolve()
      .then(loader)
      .then(
        (data) => !cancelled && setState({ data, error: null, loading: false }),
        (error) => !cancelled && setState({ data: null, error, loading: false }),
      );
    return () => {
      cancelled = true;
    };
  }, [...deps, tick]);
  return { ...state, reload: () => setTick((n) => n + 1) };
}

export function useDebounced(value, ms = 250) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return debounced;
}

/** The viewer's offset from UTC in minutes, east positive: what the server needs to know which day a play was on. */
export const viewerOffset = () => -new Date().getTimezoneOffset();

// ---------------------------------------------------------------- formatting

export const fmt = {
  time(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
    const total = Math.floor(seconds);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = String(total % 60).padStart(2, '0');
    return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${secs}` : `${minutes}:${secs}`;
  },
  duration(seconds) {
    const minutes = Math.round((seconds || 0) / 60);
    if (minutes < 60) return `${minutes} min`;
    const hours = Math.floor(minutes / 60);
    return `${hours} hr ${minutes % 60} min`;
  },
  number: (n) => (n ?? 0).toLocaleString(),
  plural: (n, one, many = one + 's') => `${(n ?? 0).toLocaleString()} ${n === 1 ? one : many}`,
  bytes(n) {
    if (!n) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
    return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
  },
  ago(epochSeconds) {
    if (!epochSeconds) return '';
    const seconds = Math.max(0, Date.now() / 1000 - epochSeconds);
    if (seconds < 90) return 'just now';
    const units = [[31536000, 'year'], [2592000, 'month'], [604800, 'week'], [86400, 'day'], [3600, 'hour'], [60, 'minute']];
    for (const [size, name] of units) {
      if (seconds >= size) {
        const n = Math.floor(seconds / size);
        return `${n} ${name}${n === 1 ? '' : 's'} ago`;
      }
    }
    return 'just now';
  },
  date(iso) {
    if (!iso) return '';
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  },
  kbps: (bitrate) => (bitrate ? `${Math.round(bitrate / 1000)} kbps` : ''),
};

export function hashHue(text) {
  let hash = 0;
  for (let i = 0; i < text.length; i++) hash = (hash * 31 + text.charCodeAt(i)) >>> 0;
  return hash % 360;
}

export const debounce = (fn, ms) => {
  let id;
  return (...args) => {
    clearTimeout(id);
    id = setTimeout(() => fn(...args), ms);
  };
};
