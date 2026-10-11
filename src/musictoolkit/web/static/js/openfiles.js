// Songs opened with the app from outside it: "Open with" in Explorer, a file dragged onto the program, or files dropped on
// the window. The desktop shell hands over their paths; the library adds the ones it does not have yet, and they play.
import { api, fmt } from './lib.js';
import { playQueue } from './player.js';
import { bumpLibrary, desktop, notifyError, toast } from './state.js';

const MAX_AT_ONCE = 500; // what the endpoint takes in one go

/** Add these files (and folders, for the songs inside them) to the library and play them. */
export async function openFiles(paths) {
  const list = (Array.isArray(paths) ? paths : []).filter((path) => typeof path === 'string' && path).slice(0, MAX_AT_ONCE);
  if (!list.length) return;
  try {
    const answer = await api('/library/open-files', { method: 'POST', body: { paths: list } });
    if (answer.added) bumpLibrary();
    if (answer.tracks.length) {
      await playQueue(answer.tracks, 0, { source: 'Opened files' });
      if (answer.skipped.length) toast(`${fmt.plural(answer.skipped.length, 'file')} could not be opened: ${answer.skipped[0].reason.toLowerCase()}`, 'info', 6000);
    } else if (answer.skipped.length) {
      toast(`Nothing to play: ${answer.skipped[0].reason.toLowerCase()}`, 'error', 6000);
    }
  } catch (error) {
    notifyError(error);
  }
}

/** Start listening for songs opened with the app, and for files dropped on the window. Does nothing in a plain browser. */
export function installOpenFiles() {
  if (!desktop?.takeOpenFiles) return () => {};
  desktop.takeOpenFiles().then(openFiles).catch(() => {}); // the ones that arrived before this page was ready
  const stopListening = desktop.onOpenFiles?.(openFiles); // and the ones that come later

  // Dropped files: the page may not look at where they are, but the shell can say (pathForFile).
  let depth = 0;
  const hasFiles = (event) => Array.from(event.dataTransfer?.types || []).includes('Files');
  const enter = (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    depth += 1;
    document.body.classList.add('dropping');
  };
  const over = (event) => {
    if (hasFiles(event)) event.preventDefault(); // without this the browser refuses the drop
  };
  const leave = (event) => {
    if (!hasFiles(event)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) document.body.classList.remove('dropping');
  };
  const drop = (event) => {
    depth = 0;
    document.body.classList.remove('dropping');
    if (!hasFiles(event)) return;
    event.preventDefault();
    openFiles(Array.from(event.dataTransfer.files, (file) => desktop.pathForFile?.(file)).filter(Boolean));
  };
  window.addEventListener('dragenter', enter);
  window.addEventListener('dragover', over);
  window.addEventListener('dragleave', leave);
  window.addEventListener('drop', drop);
  return () => {
    stopListening?.();
    window.removeEventListener('dragenter', enter);
    window.removeEventListener('dragover', over);
    window.removeEventListener('dragleave', leave);
    window.removeEventListener('drop', drop);
  };
}
