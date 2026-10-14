// Small shared building blocks: covers, ratings, cards, shelves, and the overlay hosts.
import { api, html, hashHue, scrollBehavior, useEffect, useLayoutEffect, useRef, useState, useStore, fmt } from './lib.js';
import { Icon } from './icons.js';
import { artSrc, fallbackIcon, itemKey } from './items.js';
import { closeMenu, dismissToast, href, enc, jobs, menu, modals, notifyError, toasts } from './state.js';

const THUMBS = [64, 128, 256, 512, 1024];
const pickThumb = (cssPixels) => THUMBS.find((t) => t >= cssPixels * (window.devicePixelRatio || 1)) || 1024;

/**
 * Cover art for a song (`trackId`), an album (`albumKey`) or anything the queue can hold (`item`: a song, a radio station or a podcast
 * episode). Draws a coloured placeholder when there is none.
 */
export function Cover({ trackId, albumKey, item, name = '', size = 160, round = false, class: className = '' }) {
  const [failed, setFailed] = useState(false);
  const src = item
    ? artSrc(item, pickThumb(size))
    : albumKey
      ? `/api/art/album/${albumKey}?size=${pickThumb(size)}`
      : trackId
        ? `/api/art/track/${trackId}?size=${pickThumb(size)}`
        : null;
  useEffect(() => setFailed(false), [src]);
  const hue = hashHue(name || (item ? itemKey(item) : String(trackId ?? albumKey ?? '')));
  const glyph = item ? fallbackIcon(item) : round ? 'user' : 'music';
  return html`<div class="cover ${round ? 'round' : ''} ${className}" style=${{ '--hue': hue }}>
    ${src && !failed
      ? html`<img src=${src} loading="lazy" decoding="async" alt="" draggable="false" onError=${() => setFailed(true)} />`
      : html`<span class="cover-fallback"><${Icon} name=${glyph} size=${Math.max(16, Math.round(size / 3.2))} /></span>`}
  </div>`;
}

export function Rating({ value = 0, onChange, size = 14 }) {
  const [hover, setHover] = useState(0);
  const shown = hover || value;
  return html`<span class="rating" onMouseLeave=${() => setHover(0)} role="group" aria-label="Rating">
    ${[1, 2, 3, 4, 5].map(
      (n) => html`<button
        class="star ${n <= shown ? 'on' : ''}"
        tabindex="-1"
        aria-label="${n} star${n === 1 ? '' : 's'}"
        onMouseEnter=${() => onChange && setHover(n)}
        onClick=${(e) => {
          e.stopPropagation();
          if (onChange) onChange(n === value ? 0 : n);
        }}
      ><${Icon} name="star" size=${size} fill=${n <= shown} /></button>`,
    )}
  </span>`;
}

export function Spinner({ label = '' }) {
  return html`<div class="spinner-wrap"><span class="spinner"></span>${label && html`<span>${label}</span>`}</div>`;
}

/** An explanation where there is nothing to show. `level` is its heading's level: 2 under a page's title, 1 when it is the whole page. */
export function Empty({ icon = 'music', title, children, level = 2 }) {
  const Heading = `h${level}`;
  return html`<div class="empty">
    <div class="empty-icon"><${Icon} name=${icon} size=${34} /></div>
    <${Heading}>${title}</${Heading}>
    ${children && html`<div class="empty-body">${children}</div>`}
  </div>`;
}

export function PageHeader({ title, subtitle, children }) {
  return html`<header class="page-header">
    <div>
      <h1>${title}</h1>
      ${subtitle && html`<p class="subtle">${subtitle}</p>`}
    </div>
    <div class="page-actions">${children}</div>
  </header>`;
}

export function Button({ icon, children, kind = '', onClick, title, disabled, type = 'button', small }) {
  return html`<button
    class="btn ${kind} ${small ? 'small' : ''}"
    type=${type}
    onClick=${onClick}
    title=${title}
    aria-label=${!children && title ? title : undefined}
    disabled=${disabled}
  >
    ${icon && html`<${Icon} name=${icon} size=${small ? 15 : 17} />`}${children && html`<span>${children}</span>`}
  </button>`;
}

/** The running background job of one of these kinds: what it is doing, how far along, and a Cancel button. */
export function JobBanner({ kinds }) {
  const { items } = useStore(jobs);
  const job = items.find((j) => kinds.includes(j.kind) && (j.status === 'running' || j.status === 'queued'));
  if (!job) return null;
  const pct = job.total ? Math.round((job.done / job.total) * 100) : null;
  return html`<div class="job-banner" role="status">
    <span class="spinner small"></span>
    <div class="job-banner-body">
      <strong>${job.title}${pct !== null ? ` · ${pct}%` : ''}</strong>
      <div class="progress"><div style=${{ width: (pct ?? 8) + '%' }}></div></div>
      <span class="subtle">${job.message || (job.status === 'queued' ? 'Waiting for the previous task to finish…' : '')}</span>
    </div>
    <button class="chip-btn" onClick=${() => api(`/jobs/${job.id}/cancel`, { method: 'POST' }).catch(notifyError)}>Cancel</button>
  </div>`;
}

export function IconButton({ icon, title, onClick, active, size = 18, class: className = '' }) {
  return html`<button class="icon-btn ${active ? 'active' : ''} ${className}" title=${title} aria-label=${title} onClick=${onClick}>
    <${Icon} name=${icon} size=${size} />
  </button>`;
}

// ------------------------------------------------------------ tabs

/**
 * A row of tabs (the WAI-ARIA tabs pattern): one tab stop, the arrow keys, Home and End move between the tabs (and choose
 * them), and the panel they control, a <TabPanel> with the same `id`, is named by the selected tab.
 * `tabs` are { id, label, icon?, count? }; the count is shown after the label unless it is undefined.
 */
export function Tabs({ id, label, tabs, value, onChange }) {
  const buttons = useRef({});
  const selected = tabs.some((t) => t.id === value) ? value : tabs[0]?.id;
  function onKeyDown(event) {
    const at = tabs.findIndex((t) => t.id === selected);
    const to = { ArrowRight: at + 1, ArrowLeft: at - 1, Home: 0, End: tabs.length - 1 }[event.key];
    if (to === undefined) return;
    event.preventDefault();
    const next = tabs[(to + tabs.length) % tabs.length];
    onChange(next.id);
    buttons.current[next.id]?.focus();
  }
  return html`<div class="tabs" role="tablist" aria-label=${label} onKeyDown=${onKeyDown}>
    ${tabs.map((t) => html`<button type="button" role="tab" key=${t.id} id=${`${id}-tab-${t.id}`} aria-controls=${`${id}-panel`}
      class="tab ${selected === t.id ? 'active' : ''}" aria-selected=${selected === t.id} tabIndex=${selected === t.id ? 0 : -1}
      ref=${(el) => { buttons.current[t.id] = el; }} onClick=${() => onChange(t.id)}>
      ${t.icon && html`<${Icon} name=${t.icon} size=${15} />`} ${t.label}${t.count !== undefined && html` <span class="count">${t.count}</span>`}</button>`)}
  </div>`;
}

/** What the selected tab shows. */
export function TabPanel({ id, value, class: className, children }) {
  return html`<div role="tabpanel" class=${className} id=${`${id}-panel`} aria-labelledby=${`${id}-tab-${value}`}>${children}</div>`;
}

// ------------------------------------------------------------ cards

export function AlbumCard({ album, onPlay }) {
  return html`<a class="card" href=${href(`/album/${album.key}`)} title="${album.album} - ${album.artist}">
    <div class="card-art">
      <${Cover} albumKey=${album.key} name=${album.album} size=200 />
      ${onPlay &&
      html`<button class="card-play" aria-label="Play ${album.album}" onClick=${(e) => { e.preventDefault(); e.stopPropagation(); onPlay(album); }}>
        <${Icon} name="play" size=${22} />
      </button>`}
    </div>
    <div class="card-title">${album.album}</div>
    <div class="card-sub">${[album.year, album.artist].filter(Boolean).join(' · ')}</div>
  </a>`;
}

export function ArtistCard({ artist }) {
  return html`<a class="card artist" href=${href(`/artist/${enc(artist.name)}`)}>
    <div class="card-art"><${Cover} trackId=${artist.cover_track_id} name=${artist.name} size=200 round /></div>
    <div class="card-title">${artist.name}</div>
    <div class="card-sub">${fmt.plural(artist.albums ?? 0, 'album')} · ${fmt.plural(artist.tracks, 'song')}</div>
  </a>`;
}

export function PlaylistCard({ playlist, onPlay }) {
  const ids = playlist.cover_track_ids || [];
  return html`<a class="card" href=${href(`/playlist/${playlist.id}`)}>
    <div class="card-art">
      <div class="mosaic ${ids.length < 4 ? 'single' : ''}" style=${{ '--hue': hashHue(playlist.name) }}>
        ${ids.length === 0
          ? html`<span class="cover-fallback"><${Icon} name=${playlist.kind === 'smart' ? 'sparkles' : 'list'} size=${44} /></span>`
          : ids.length < 4
            ? html`<${Cover} trackId=${ids[0]} name=${playlist.name} size=200 />`
            : ids.map((id) => html`<${Cover} trackId=${id} name=${playlist.name} size=100 />`)}
      </div>
      ${onPlay &&
      html`<button class="card-play" aria-label="Play ${playlist.name}" onClick=${(e) => { e.preventDefault(); e.stopPropagation(); onPlay(playlist); }}>
        <${Icon} name="play" size=${22} />
      </button>`}
    </div>
    <div class="card-title">${playlist.name}</div>
    <div class="card-sub">
      ${playlist.kind === 'smart' ? 'Smart · ' : ''}${fmt.plural(playlist.tracks, 'song')}
    </div>
  </a>`;
}

export function TrackCard({ track, onPlay }) {
  return html`<button class="card track-card" onClick=${() => onPlay(track)} title="${track.title} - ${track.artist}">
    <div class="card-art">
      <${Cover} albumKey=${track.album_key} name=${track.album} size=200 />
      <span class="card-play static"><${Icon} name="play" size=${22} /></span>
    </div>
    <div class="card-title">${track.title}</div>
    <div class="card-sub">${track.artist}</div>
  </button>`;
}

/** A titled, horizontally scrolling shelf of cards. */
export function Shelf({ title, subtitle, to, children }) {
  const ref = useRef();
  const scroll = (dir) => ref.current?.scrollBy({ left: dir * ref.current.clientWidth * 0.85, behavior: scrollBehavior() });
  return html`<section class="shelf">
    <div class="shelf-head">
      <h2>${to ? html`<a href=${href(to)}>${title}</a>` : title}</h2>
      ${subtitle && html`<span class="subtle">${subtitle}</span>`}
      <span class="spacer"></span>
      <button class="icon-btn" aria-label="Scroll left" onClick=${() => scroll(-1)}><${Icon} name="chevron-left" size=${18} /></button>
      <button class="icon-btn" aria-label="Scroll right" onClick=${() => scroll(1)}><${Icon} name="chevron-right" size=${18} /></button>
    </div>
    <div class="shelf-row" ref=${ref}>${children}</div>
  </section>`;
}

/** Call `onVisible` when the sentinel scrolls into view: infinite scrolling for card grids. */
export function InfiniteSentinel({ onVisible, disabled }) {
  const ref = useRef();
  useEffect(() => {
    if (disabled || !ref.current) return undefined;
    const observer = new IntersectionObserver((entries) => entries[0].isIntersecting && onVisible(), { rootMargin: '600px' });
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, [disabled, onVisible]);
  return html`<div ref=${ref} class="sentinel" aria-hidden="true"></div>`;
}

// ------------------------------------------------------------ overlay hosts

export function ToastHost() {
  const { items } = useStore(toasts);
  const show = (t) => html`<div class="toast ${t.kind}" key=${t.id}>
    <${Icon} name=${t.kind === 'error' ? 'alert' : t.kind === 'success' ? 'check' : 'info'} size=${16} />
    <span>${t.text}</span>
    ${t.action && html`<button class="toast-action" onClick=${() => { dismissToast(t.id); t.action.onClick(); }}>${t.action.label}</button>`}
    <button class="icon-btn" aria-label="Dismiss" onClick=${() => dismissToast(t.id)}><${Icon} name="x" size=${14} /></button>
  </div>`;
  // Two live regions that are always on the page, so that a screen reader says what lands in them: a problem at once, the rest when it has finished speaking.
  return html`<div class="toasts">
    <div role="status" aria-live="polite">${items.filter((t) => t.kind !== 'error').map(show)}</div>
    <div role="alert">${items.filter((t) => t.kind === 'error').map(show)}</div>
  </div>`;
}

export function MenuHost() {
  const state = useStore(menu);
  const ref = useRef();
  const [pos, setPos] = useState({ left: 0, top: 0 });

  useLayoutEffect(() => {
    if (!state.open) return undefined;
    const close = () => closeMenu();
    const onKey = (e) => e.key === 'Escape' && closeMenu({ restore: true });
    window.addEventListener('click', close);
    window.addEventListener('blur', close);
    window.addEventListener('resize', close);
    window.addEventListener('keydown', onKey);
    document.addEventListener('scroll', close, true);
    return () => {
      window.removeEventListener('click', close);
      window.removeEventListener('blur', close);
      window.removeEventListener('resize', close);
      window.removeEventListener('keydown', onKey);
      document.removeEventListener('scroll', close, true);
    };
  }, [state.open]);

  useEffect(() => {
    if (!state.open || !ref.current) return;
    const box = ref.current.getBoundingClientRect();
    setPos({
      left: Math.max(8, Math.min(state.x, window.innerWidth - box.width - 8)),
      top: Math.max(8, Math.min(state.y, window.innerHeight - box.height - 8)),
    });
  }, [state.open, state.x, state.y, state.items]);

  // An open menu has the focus: the arrow keys move through it, Escape closes it and gives the focus back.
  useLayoutEffect(() => {
    if (state.open) ref.current?.querySelector('[role="menuitem"]:not(:disabled)')?.focus({ preventScroll: true });
  }, [state.open, state.items]);

  function onKeyDown(event) {
    const items = [...ref.current.querySelectorAll('[role="menuitem"]:not(:disabled)')];
    const at = items.indexOf(document.activeElement);
    const to = { ArrowDown: at + 1, ArrowUp: Math.max(at, 0) - 1, Home: 0, End: items.length - 1 }[event.key];
    if (to !== undefined && items.length) {
      event.preventDefault();
      items[(to + items.length) % items.length].focus();
    } else if (event.key === 'Tab') {
      closeMenu({ restore: true }); // the focus goes back where it was, and Tab carries on from there
    }
  }

  if (!state.open) return null;
  return html`<div class="menu" ref=${ref} style=${{ left: pos.left + 'px', top: pos.top + 'px' }} role="menu" aria-label="Actions"
    onKeyDown=${onKeyDown} onClick=${(e) => e.stopPropagation()} onContextMenu=${(e) => e.preventDefault()}>
    ${state.items.map((item, i) =>
      item.divider
        ? html`<div class="menu-divider" role="separator" key=${i}></div>`
        : html`<button
            key=${i}
            class="menu-item ${item.danger ? 'danger' : ''}"
            role="menuitem"
            tabIndex="-1"
            disabled=${item.disabled}
            onClick=${() => { closeMenu({ restore: true }); item.action?.(); }}
          >
            <${Icon} name=${item.icon || 'chevron-right'} size=${15} class=${item.icon ? '' : 'invisible'} />
            <span>${item.label}</span>
            ${item.hint && html`<kbd>${item.hint}</kbd>`}
          </button>`,
    )}
  </div>`;
}

// What can take the focus inside a dialog (and is on screen).
const FOCUSABLE = 'a[href], button, input, select, textarea, [tabindex]';
const reachable = (root) => (root ? [...root.querySelectorAll(FOCUSABLE)].filter((el) => !el.disabled && el.tabIndex >= 0 && el.getClientRects().length > 0) : []);

function Dialog({ m, top }) {
  const box = useRef();
  const titleId = `modal-title-${m.id}`;

  // A dialog takes the focus when it opens: whatever its content asked for, otherwise its first control, otherwise itself.
  useLayoutEffect(() => {
    const el = box.current;
    if (!el || !top || el.contains(document.activeElement)) return;
    (reachable(el.querySelector('.modal-body'))[0] || el).focus({ preventScroll: true });
  }, [top]);

  // Tab and Shift+Tab go round inside the dialog instead of out into the page behind it.
  function onKeyDown(event) {
    if (event.key !== 'Tab') return;
    const items = reachable(box.current);
    const [first, last] = [items[0], items[items.length - 1]];
    const here = document.activeElement;
    if (!first) {
      event.preventDefault();
      box.current.focus();
    } else if (event.shiftKey && (here === first || here === box.current)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && here === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return html`<div class="modal-backdrop" data-modal-id=${m.id} inert=${!top} onMouseDown=${(e) => e.target === e.currentTarget && m.close()}>
    <div class="modal" ref=${box} style=${{ maxWidth: m.width + 'px' }} role="dialog" aria-modal="true" aria-labelledby=${titleId} tabIndex="-1" onKeyDown=${onKeyDown}>
      <div class="modal-head">
        <h2 id=${titleId}>${m.title}</h2>
        <button class="icon-btn" aria-label="Close" onClick=${m.close}><${Icon} name="x" size=${18} /></button>
      </div>
      <div class="modal-body">${m.render(m.close)}</div>
    </div>
  </div>`;
}

export function ModalHost() {
  const { stack } = useStore(modals);
  const top = stack[stack.length - 1];
  useLayoutEffect(() => {
    if (!top) return undefined;
    const onKey = (e) => e.key === 'Escape' && top.close();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [top?.id]);
  if (!stack.length) return null;
  return html`${stack.map((m) => html`<${Dialog} key=${m.id} m=${m} top=${m === top} />`)}`;
}
