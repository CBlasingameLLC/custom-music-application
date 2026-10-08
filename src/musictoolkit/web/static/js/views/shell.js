// The frame around every view: sidebar, top bar, task tray, and the bottom player bar.
import { api, fmt, html, useEffect, useRef, useState, useStore } from '../lib.js';
import { Icon } from '../icons.js';
import { Cover, IconButton } from '../components.js';
import { current, cycleRepeat, next, player, previous, seek, setVolume, toggle, toggleMute, toggleShuffle } from '../player.js';
import { QueueList } from './nowplaying.js';
import { setFavorite } from '../tracks.js';
import { enc, go, href, jobs, library, loadPlaylists, notifyError, route, setTheme, ui } from '../state.js';

// ------------------------------------------------------------ sidebar

function NavItem({ to, icon, label, match }) {
  const { path } = useStore(route);
  const active = match ? match(path) : path === to;
  return html`<a class="nav-item ${active ? 'active' : ''}" href=${href(to)} aria-current=${active ? 'page' : undefined}>
    <${Icon} name=${icon} size=${18} /><span>${label}</span></a>`;
}

export function Sidebar() {
  const playlists = useStore(library, (s) => s.playlists);
  const about = useStore(library, (s) => s.about);
  useEffect(() => { loadPlaylists().catch(() => {}); }, []);
  const starts = (prefix) => (path) => path === prefix || path.startsWith(prefix + '/');
  return html`<nav class="sidebar" aria-label="Main">
    <a class="brand" href=${href('/')}>
      <span class="logo"><${Icon} name="music" size=${18} /></span><span>Music Toolkit</span>
    </a>
    <div class="nav-group">
      <${NavItem} to="/" icon="home" label="Home" />
      <${NavItem} to="/songs" icon="music" label="Songs" match=${starts('/songs')} />
      <${NavItem} to="/albums" icon="disc" label="Albums" match=${(p) => starts('/albums')(p) || starts('/album')(p)} />
      <${NavItem} to="/artists" icon="user" label="Artists" match=${(p) => starts('/artists')(p) || starts('/artist')(p)} />
      <${NavItem} to="/playlists" icon="list" label="Playlists" match=${(p) => starts('/playlists')(p) || starts('/playlist')(p)} />
      <${NavItem} to="/favorites" icon="heart" label="Favorites" />
    </div>
    <div class="nav-group">
      <div class="nav-label">Listen</div>
      <${NavItem} to="/recent" icon="clock" label="Recently played" />
      <${NavItem} to="/discover" icon="compass" label="Discover" />
      <${NavItem} to="/history" icon="chart" label="Listening history" />
    </div>
    <div class="nav-group">
      <div class="nav-label">Manage</div>
      <${NavItem} to="/devices" icon="drive" label="Devices" />
      <${NavItem} to="/settings" icon="sliders" label="Settings" />
    </div>
    ${playlists.length > 0 && html`<div class="nav-group playlists-nav">
      <div class="nav-label">Playlists</div>
      <div class="nav-scroll">${playlists.slice(0, 40).map((p) => html`<a class="nav-item small" href=${href(`/playlist/${p.id}`)} key=${p.id} title=${p.name}>
        <${Icon} name=${p.kind === 'smart' ? 'sparkles' : 'list'} size=${15} /><span>${p.name}</span></a>`)}</div>
    </div>`}
    <div class="sidebar-foot">${about ? `v${about.version} · ${fmt.plural(about.tracks, 'song')}` : ''}</div>
  </nav>`;
}

// ------------------------------------------------------------ top bar

function TasksPill() {
  const { items } = useStore(jobs);
  const { jobsOpen } = useStore(ui);
  const ref = useRef();
  const active = items.filter((j) => j.status === 'queued' || j.status === 'running');
  useEffect(() => {
    if (!jobsOpen) return undefined;
    const close = (e) => !ref.current?.contains(e.target) && ui.set({ jobsOpen: false });
    window.addEventListener('mousedown', close);
    return () => window.removeEventListener('mousedown', close);
  }, [jobsOpen]);
  const lead = active[0];
  const pct = lead && lead.total ? Math.round((lead.done / lead.total) * 100) : null;
  return html`<div class="tasks" ref=${ref}>
    <button class="tasks-pill ${active.length ? 'busy' : ''}" aria-label="Background tasks" aria-expanded=${jobsOpen} onClick=${() => ui.set({ jobsOpen: !jobsOpen })}>
      ${active.length ? html`<span class="spinner small"></span><span>${lead.title}${pct !== null ? ` · ${pct}%` : ''}</span>` : html`<${Icon} name="check" size=${15} /><span>No tasks running</span>`}
    </button>
    ${jobsOpen && html`<div class="tasks-popover" role="dialog" aria-label="Background tasks">
      ${items.length === 0 && html`<p class="subtle pad">Nothing has run yet. Scans, tag lookups, and device syncs show up here.</p>`}
      ${items.slice(0, 8).map((job) => html`<div class="task ${job.status}" key=${job.id}>
        <div class="task-head">
          <${Icon} name=${job.status === 'done' ? 'check' : job.status === 'error' ? 'alert' : job.status === 'cancelled' ? 'x' : 'refresh'} size=${15} />
          <strong>${job.title}</strong><span class="spacer"></span>
          ${(job.status === 'running' || job.status === 'queued') && html`<button class="chip-btn" onClick=${() => api(`/jobs/${job.id}/cancel`, { method: 'POST' }).catch(notifyError)}>Cancel</button>`}
        </div>
        ${job.status === 'running' && html`<div class="progress"><div style=${{ width: (job.total ? (job.done / job.total) * 100 : 8) + '%' }}></div></div>`}
        <div class="subtle">${job.status === 'error' ? job.error : job.status === 'running' || job.status === 'queued' ? job.message || (job.status === 'queued' ? 'Waiting…' : '') : job.status === 'cancelled' ? 'Cancelled' : summarize(job)}</div>
      </div>`)}
    </div>`}
  </div>`;
}

function summarize(job) {
  const r = job.result || {};
  if (job.kind === 'scan') return `${r.added || 0} added · ${r.updated || 0} changed · ${r.unchanged || 0} unchanged${r.missing ? ` · ${r.missing} missing` : ''}${r.offline_roots?.length ? ` · ${r.offline_roots.length} folder(s) not found` : ''}`;
  return job.message || 'Finished';
}

export function TopBar() {
  const { path, parts } = useStore(route);
  const [term, setTerm] = useState(parts[0] === 'search' ? parts[1] || '' : '');
  const input = useRef();
  const { theme } = useStore(ui);

  // The box mirrors the results page; leaving it (e.g. clicking an artist in the results) clears it.
  useEffect(() => { setTerm(parts[0] === 'search' ? parts[1] || '' : ''); }, [path]);
  useEffect(() => {
    const focus = (e) => {
      if (e.key === '/' && !e.target.closest('input, textarea, select')) { e.preventDefault(); input.current?.focus(); input.current?.select(); }
    };
    window.addEventListener('keydown', focus);
    return () => window.removeEventListener('keydown', focus);
  }, []);

  const search = (value) => {
    setTerm(value);
    if (value.trim()) go(`/search/${enc(value)}`);
    else if (parts[0] === 'search') go('/');
  };

  return html`<header class="topbar">
    <div class="nav-arrows">
      <${IconButton} icon="chevron-left" title="Back" onClick=${() => history.back()} />
      <${IconButton} icon="chevron-right" title="Forward" onClick=${() => history.forward()} />
    </div>
    <label class="search-box top">
      <${Icon} name="search" size=${16} />
      <input ref=${input} type="search" value=${term} placeholder="Search your library   ( / )" aria-label="Search your library"
        onInput=${(e) => search(e.target.value)} onKeyDown=${(e) => e.key === 'Escape' && (setTerm(''), e.target.blur())} />
    </label>
    <span class="spacer"></span>
    <${TasksPill} />
    <${IconButton} icon=${theme === 'dark' ? 'sun' : 'moon'} title=${theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'} onClick=${() => setTheme(theme === 'dark' ? 'light' : 'dark')} />
  </header>`;
}

// ------------------------------------------------------------ player bar

function Seek({ track }) {
  const position = useStore(player, (s) => s.position);
  const duration = useStore(player, (s) => s.duration || track?.duration || 0);
  const [dragging, setDragging] = useState(null);
  const shown = dragging ?? position;
  const pct = duration ? Math.min(100, (shown / duration) * 100) : 0;
  return html`<div class="seek">
    <span class="time">${fmt.time(shown)}</span>
    <input class="slider" type="range" min="0" max=${duration || 0} step="0.1" value=${shown} disabled=${!track} aria-label="Seek"
      style=${{ '--pct': pct + '%' }}
      onInput=${(e) => setDragging(Number(e.target.value))}
      onChange=${(e) => { seek(Number(e.target.value)); setDragging(null); }} />
    <span class="time">${fmt.time(duration)}</span>
  </div>`;
}

export function PlayerBar() {
  const s = useStore(player);
  const track = current(s);
  const { queueOpen } = useStore(ui);
  const volumeIcon = s.muted || s.volume === 0 ? 'volume-mute' : s.volume < 0.4 ? 'volume-low' : 'volume';

  return html`<footer class="playerbar" aria-label="Player">
    <div class="pb-track">
      ${track
        ? html`<a class="pb-art" href=${href('/now')} aria-label="Open Now Playing"><${Cover} trackId=${track.id} name=${track.album} size=56 /></a>
            <div class="pb-text">
              <a class="t1" href=${href('/now')} title=${track.title}>${track.title}</a>
              <a class="t2" href=${href(`/artist/${enc(track.album_artist || track.artist)}`)}>${track.artist}</a>
            </div>
            <button class="icon-btn heart ${track.favorite ? 'on' : ''}" aria-label=${track.favorite ? 'Remove from favorites' : 'Add to favorites'} aria-pressed=${track.favorite}
              onClick=${() => setFavorite([track], !track.favorite).catch(notifyError)}><${Icon} name="heart" size=${18} fill=${track.favorite} /></button>`
        : html`<div class="pb-idle subtle">Nothing playing</div>`}
    </div>

    <div class="pb-center">
      <div class="controls">
        <${IconButton} icon="shuffle" title=${s.shuffle ? 'Shuffle: on' : 'Shuffle: off'} active=${s.shuffle} onClick=${toggleShuffle} />
        <${IconButton} icon="prev" title="Previous" size=${20} onClick=${previous} />
        <button class="play-btn" aria-label=${s.playing ? 'Pause' : 'Play'} disabled=${!track} onClick=${toggle}>
          ${s.loading && s.playing ? html`<span class="spinner small"></span>` : html`<${Icon} name=${s.playing ? 'pause' : 'play'} size=${22} />`}
        </button>
        <${IconButton} icon="next" title="Next" size=${20} onClick=${next} />
        <${IconButton} icon=${s.repeat === 'one' ? 'repeat-one' : 'repeat'} title=${`Repeat: ${s.repeat}`} active=${s.repeat !== 'off'} onClick=${cycleRepeat} />
      </div>
      <${Seek} track=${track} />
    </div>

    <div class="pb-right">
      <${IconButton} icon="mic" title="Now Playing and lyrics" onClick=${() => go('/now')} />
      <${IconButton} icon="queue" title="Queue" active=${queueOpen} onClick=${() => ui.set({ queueOpen: !queueOpen })} />
      <${IconButton} icon=${volumeIcon} title=${s.muted ? 'Unmute' : 'Mute'} onClick=${toggleMute} />
      <input class="slider volume" type="range" min="0" max="1" step="0.01" value=${s.muted ? 0 : s.volume} aria-label="Volume"
        style=${{ '--pct': (s.muted ? 0 : s.volume) * 100 + '%' }} onInput=${(e) => setVolume(Number(e.target.value))} />
    </div>
  </footer>`;
}

export function QueueDrawer() {
  const { queueOpen } = useStore(ui);
  if (!queueOpen) return null;
  return html`<aside class="queue-drawer" aria-label="Queue">
    <div class="drawer-head"><h2>Queue</h2><${IconButton} icon="x" title="Close queue" onClick=${() => ui.set({ queueOpen: false })} /></div>
    <${QueueList} />
  </aside>`;
}
