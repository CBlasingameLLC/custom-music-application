// Entry point: layout, routing, keyboard shortcuts, startup.
import { html, render, useEffect, useRef, useStore } from './lib.js';
import { Empty, MenuHost, ModalHost, ToastHost } from './components.js';
import { cycleRepeat, current, next, pause, player, previous, restoreQueue, seekBy, setVolume, toggle, toggleMute, toggleShuffle, updateTrack } from './player.js';
import { desktop, go, loadAbout, loadFacets, loadPlaylists, loadSettings, route, startJobPolling, ui } from './state.js';
import { setFavorite } from './tracks.js';
import { PlayerBar, QueueDrawer, Sidebar, TopBar } from './views/shell.js';
import { AlbumsView, AlbumView, ArtistsView, ArtistView, HomeView, SearchView, SongsView } from './views/library.js';
import { PlaylistsView, PlaylistView } from './views/playlists.js';
import { NowPlayingView } from './views/nowplaying.js';
import { SettingsView } from './views/settings.js';
import { DiscoverView, HistoryView } from './views/discover.js';
import { StatsView } from './views/stats.js';
import { MixesView, MixView } from './views/mixes.js';
import { DeviceView, DevicesView } from './views/devices.js';
import { ImportView } from './views/importer.js';
import { EnrichView, ToolsView } from './views/tools.js';
import { OrganizeView } from './views/organize.js';
import { DuplicatesView } from './views/duplicates.js';
import { MissingView } from './views/missing.js';

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
    default: return html`<${Empty} icon="search" title="That page doesn't exist"><a href="#/">Go home</a><//>`;
  }
}

function useShortcuts() {
  useEffect(() => {
    const onKey = (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.target.closest('input, textarea, select, [contenteditable="true"]')) return;
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
        case 'l': {
          const track = current(s);
          if (track) setFavorite([track], !track.favorite).catch(() => {});
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
}

function App() {
  const { path } = useStore(route);
  const view = useRef();
  const track = useStore(player, (s) => current(s));
  const playing = useStore(player, (s) => s.playing);
  useShortcuts();
  useEffect(() => view.current?.scrollTo(0, 0), [path]);
  useEffect(() => {
    document.title = track ? `${playing ? '▶ ' : ''}${track.title} · ${track.artist}` : 'Music Toolkit';
  }, [track?.id, playing]);

  return html`<div class="app">
    <${Sidebar} />
    <div class="main-col">
      <${TopBar} />
      <main class="view" ref=${view} tabIndex="-1"><${View} /></main>
    </div>
    <${QueueDrawer} />
    <${PlayerBar} />
    <${ToastHost} /><${MenuHost} /><${ModalHost} />
  </div>`;
}

render(html`<${App} />`, document.getElementById('app'));
startJobPolling();
loadAbout().catch(() => {});
loadSettings().catch(() => {});
loadFacets().catch(() => {});
loadPlaylists().catch(() => {});
restoreQueue();
