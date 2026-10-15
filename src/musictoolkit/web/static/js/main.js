// Entry point: layout, routing, keyboard shortcuts, startup.
import { Fragment, html, render, useEffect, useLayoutEffect, useRef, useState, useStore } from './lib.js';
import { Empty, MenuHost, ModalHost, ToastHost } from './components.js';
import { cycleRepeat, current, next, pause, player, previous, restoreQueue, seekBy, setVolume, toggle, toggleMute, toggleShuffle, updateTrack } from './player.js';
import { desktop, go, loadAbout, loadFacets, loadPlaylists, loadSettings, modals, route, startJobPolling, toast, ui } from './state.js';
import { setFavorite } from './tracks.js';
import { describe, isStation, isTrack } from './items.js';
import { setStationFavorite } from './radio.js';
import { installOpenFiles } from './openfiles.js';
import { PlayerBar, QueueDrawer, Sidebar, TopBar } from './views/shell.js';
import { AlbumsView, AlbumView, ArtistsView, ArtistView, HomeView, SearchView, SongsView } from './views/library.js';
import { PlaylistsView, PlaylistView } from './views/playlists.js';
import { NowPlayingView } from './views/nowplaying.js';
import { SettingsView } from './views/settings.js';
import { DiscoverView, HistoryView } from './views/discover.js';
import { StatsView } from './views/stats.js';
import { MixesView, MixView } from './views/mixes.js';
import { DeviceView, DevicesView } from './views/devices.js';
import { DiscsView } from './views/discs.js';
import { ImportView } from './views/importer.js';
import { EnrichView, ToolsView } from './views/tools.js';
import { OrganizeView } from './views/organize.js';
import { DuplicatesView } from './views/duplicates.js';
import { MissingView } from './views/missing.js';
import { RadioView } from './views/radio.js';
import { PodcastsView, PodcastView } from './views/podcasts.js';

function View() {
  const { parts, query } = useStore(route);
  const [first, second] = parts;
  switch (first) {
    case undefined: return html`<${HomeView} />`;
    case 'songs': return html`<${SongsView} key=${'songs' + (query.artist || '')} />`;
    case 'favorites': return html`<${SongsView} key="favorites" preset="favorites" />`;
    case 'recent': return html`<${SongsView} key="recent" preset="recent" />`;
    case 'albums': return html`<${AlbumsView} key=${'albums' + (query.sort || '')} />`;
    case 'album': return html`<${AlbumView} key=${second} albumKey=${second} />`;
    case 'artists': return html`<${ArtistsView} />`;
    case 'artist': return html`<${ArtistView} key=${second} name=${second} />`;
    case 'playlists': return html`<${PlaylistsView} />`;
    case 'playlist': return html`<${PlaylistView} key=${second} id=${second} />`;
    case 'now': return html`<${NowPlayingView} />`;
    case 'search': return html`<${SearchView} term=${second || ''} />`;
    case 'discover': return html`<${DiscoverView} />`;
    case 'devices': return second ? html`<${DeviceView} key=${second} id=${second} />` : html`<${DevicesView} />`;
    case 'discs': return html`<${DiscsView} />`;
    case 'radio': return html`<${RadioView} />`;
    case 'podcasts': return html`<${PodcastsView} />`;
    case 'podcast': return html`<${PodcastView} key=${second} id=${second} />`;
    case 'mixes': return html`<${MixesView} />`;
    case 'mix': return html`<${MixView} key=${second} id=${second} />`;
    case 'stats': return html`<${StatsView} key=${query.range || ''} />`;
    case 'history': return second === 'import' ? html`<${ImportView} />` : html`<${HistoryView} />`;
    case 'tools':
      if (second === 'enrich') return html`<${EnrichView} />`;
      if (second === 'organize') return html`<${OrganizeView} />`;
      if (second === 'duplicates') return html`<${DuplicatesView} />`;
      if (second === 'missing') return html`<${MissingView} />`;
      return html`<${ToolsView} />`;
    case 'settings': return html`<${SettingsView} />`;
    default: return html`<${Empty} icon="search" title="That page doesn't exist" level=${1}><a href="#/">Go home</a><//>`;
  }
}

function useShortcuts() {
  useEffect(() => {
    const onKey = (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.target.closest('input, textarea, select, [contenteditable="true"]')) return;
      if (modals.get().stack.length) return; // a dialog has the keyboard to itself
      const onButton = e.target.closest('button, a, [role="tab"], [role="menuitem"]');
      const s = player.get();
      switch (e.key) {
        case ' ':
          if (onButton) return;
          e.preventDefault(); toggle(); break;
        case 'ArrowRight': if (e.shiftKey) next(); else if (!onButton) seekBy(5); else return; e.preventDefault(); break;
        case 'ArrowLeft': if (e.shiftKey) previous(); else if (!onButton) seekBy(-5); else return; e.preventDefault(); break;
        case 'ArrowUp': if (!e.shiftKey) return; e.preventDefault(); setVolume(s.volume + 0.05); break;
        case 'ArrowDown': if (!e.shiftKey) return; e.preventDefault(); setVolume(s.volume - 0.05); break;
        case 'm': toggleMute(); break;
        case 's': toggleShuffle(); break;
        case 'r': cycleRepeat(); break;
        case 'q': ui.set({ queueOpen: !ui.get().queueOpen }); break;
        case 'n': go('/now'); break;
        case 'l': { // favorite what is playing: a song, or a radio station
          const item = current(s);
          if (item && isTrack(item)) setFavorite([item], !item.favorite).catch(() => {});
          else if (item && isStation(item)) setStationFavorite(item, !item.favorite).catch(() => {});
          break;
        }
        default: return;
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  // Hardware media keys, forwarded by the desktop shell.
  useEffect(() => desktop?.onMediaKey?.((key) => ({ playpause: toggle, next, previous, stop: pause })[key]?.()), []);

  // Tell the desktop shell what is playing: the tray's tooltip and menu, and whether closing the window keeps the music going.
  useEffect(() => {
    if (!desktop?.setNowPlaying) return undefined;
    let last = '';
    const send = (s) => {
      const item = current(s);
      const shown = item ? describe(item, s.liveTitle) : null; // a station shows what it is playing
      const info = { title: shown?.title || '', artist: shown?.subtitle || '', playing: Boolean(s.playing && item) };
      const key = JSON.stringify(info);
      if (key === last) return; // the position changes four times a second; this only cares about the song and playing
      last = key;
      desktop.setNowPlaying(info).catch(() => {});
    };
    send(player.get());
    return player.subscribe(send);
  }, []);

  // Songs opened with the app (Explorer's Open with, a file dragged onto the program) and files dropped on the window.
  useEffect(() => installOpenFiles(), []);
}

/**
 * When the page changes: move the keyboard to the page (unless someone is typing, as in the search box), and say which page
 * it is, for a screen reader. Returns the sentence to say. Not on the first page: that is the app opening.
 */
function useRouteAnnouncer(view, path) {
  const [said, setSaid] = useState('');
  const first = useRef(true);
  useLayoutEffect(() => { // before the new page is painted: a key pressed once it is on screen finds the focus already there
    if (first.current) {
      first.current = false;
      return undefined;
    }
    const page = view.current;
    const typing = document.activeElement?.matches?.('input, textarea, select, [contenteditable="true"]');
    if (!page || typing) return undefined;
    page.focus({ preventScroll: true });
    const title = () => page.querySelector('h1')?.textContent?.trim();
    // A page that loads its content shows its title a moment later: wait for it, but not for ever.
    const observer = new MutationObserver(() => title() && (setSaid(title()), observer.disconnect()));
    if (title()) setSaid(title());
    else observer.observe(page, { childList: true, subtree: true });
    const giveUp = setTimeout(() => observer.disconnect(), 4000);
    return () => {
      clearTimeout(giveUp);
      observer.disconnect();
    };
  }, [path]);
  return said;
}

function App() {
  const { path } = useStore(route);
  const view = useRef();
  const item = useStore(player, (s) => current(s));
  const liveTitle = useStore(player, (s) => s.liveTitle);
  const playing = useStore(player, (s) => s.playing);
  const dialogOpen = useStore(modals, (s) => s.stack.length > 0);
  useShortcuts();
  useEffect(() => view.current?.scrollTo(0, 0), [path]);
  const pageName = useRouteAnnouncer(view, path);
  useEffect(() => {
    const shown = item ? describe(item, liveTitle) : null;
    document.title = shown ? `${playing ? '▶ ' : ''}${shown.title} · ${shown.subtitle}` : 'Music Toolkit';
  }, [item?.id, item?.kind, liveTitle, playing]);

  // A song, station or episode that cannot be played says so: the player only keeps the reason.
  useEffect(() => {
    let said = null;
    return player.subscribe((s) => {
      if (s.error && s.error !== said) toast(s.error, 'error', 9000);
      said = s.error;
    });
  }, []);

  // While a dialog is open the rest of the app is inert: no focus, no clicks, and not read out behind the dialog.
  return html`<${Fragment}>
    <div class="app" inert=${dialogOpen}>
      <button type="button" class="skip-link" onClick=${() => view.current?.focus()}>Skip to content</button>
      <${Sidebar} />
      <div class="main-col">
        <${TopBar} />
        <main class="view" id="main" ref=${view} tabIndex="-1" data-route=${path}><${View} /><${MenuHost} /></main>
      </div>
      <${QueueDrawer} />
      <${PlayerBar} />
    </div>
    <${ToastHost} /><${ModalHost} />
    <div class="sr-only" role="status" aria-live="polite" aria-atomic="true">${pageName}</div>
  <//>`;
}

// The page starts with a "Loading" line (shown until this script runs); the app replaces it.
const root = document.getElementById('app');
root.replaceChildren();
render(html`<${App} />`, root);
startJobPolling();
loadAbout().catch(() => {});
loadSettings().catch(() => {});
loadFacets().catch(() => {});
loadPlaylists().catch(() => {});
restoreQueue();
