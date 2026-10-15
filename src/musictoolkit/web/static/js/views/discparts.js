// What the Rip and Burn tabs of the Discs screen have in common: a date in words, and a look at the drives every few seconds.
import { api, useCallback, useEffect, useState } from '../lib.js';

const WATCH_MS = 3000; // how often the drives are looked at while the page is open

export const when = (iso) => {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : `${date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}, ${date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}`;
};

/** The CD and DVD drives and what is in them, looked at again every few seconds while `watching`. `listed` says whether what is in `data` was
 * seen since the watching began: a list from before a rip, say, may name a disc that the rip has ejected. */
export function useDrives(watching) {
  const [state, setState] = useState({ loading: true, data: null, error: null, listed: false });
  const look = useCallback(async (fresh) => {
    try {
      const data = await api('/discs/drives', { params: fresh ? { fresh: true } : {} });
      setState((before) => {
        // Looking failed this time (the helper did not answer): what was seen before stays on the screen, with the trouble above it.
        if (data.error && !data.drives.length && before.data?.drives.length) return { ...before, loading: false, error: new Error(data.error) };
        return JSON.stringify(before.data) === JSON.stringify(data) && !before.error && before.listed ? before : { loading: false, data, error: null, listed: true };
      });
    } catch (error) {
      setState((before) => ({ ...before, loading: false, error }));
    }
  }, []);
  useEffect(() => {
    if (!watching) {
      setState((before) => (before.listed ? { ...before, listed: false } : before));
      return undefined;
    }
    look(true);
    const timer = setInterval(() => !document.hidden && look(false), WATCH_MS);
    const onFocus = () => look(true); // coming back to the window after putting a disc in
    window.addEventListener('focus', onFocus);
    return () => {
      clearInterval(timer);
      window.removeEventListener('focus', onFocus);
    };
  }, [watching, look]);
  return [state, look];
}
