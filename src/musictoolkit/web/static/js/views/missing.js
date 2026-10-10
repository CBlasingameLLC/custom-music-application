// Missing files: songs the library lists but whose file cannot be found. Look at them, or forget them for good.
import { api, fmt, html, useAsync, useEffect, useState, useStore } from '../lib.js';
import { Icon } from '../icons.js';
import { Button, Empty, JobBanner, PageHeader, Spinner } from '../components.js';
import { confirmDialog } from '../dialogs.js';
import { bumpLibrary, jobs, library, loadPlaylists, notifyError, toast, trackJob } from '../state.js';

const OTHER = '*other*';
const PAGE = 100;

const shorten = (path) => {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.length > 4 ? `…/${parts.slice(-4).join('/')}` : parts.join('/');
};

function FolderCard({ title, count, available, note, active, busy, onShow, onForget }) {
  return html`<div class="folder-card ${active ? 'active' : ''}">
    <div class="folder-card-head">
      <strong title=${title}>${shorten(title)}</strong>
      ${available === true && html`<span class="chip good">Connected</span>`}
      ${available === false && html`<span class="chip ok">Not connected</span>`}
    </div>
    <p class="subtle">${fmt.plural(count, 'song')} cannot be found${note ? ` · ${note}` : ''}</p>
    <div class="row-actions">
      <${Button} small onClick=${onShow}>${active ? 'Showing' : 'Show'}<//>
      <${Button} small kind="danger-ghost" icon="trash" disabled=${busy || !count} onClick=${onForget}>Forget…<//>
    </div>
  </div>`;
}

export function MissingView() {
  const rev = useStore(library, (s) => s.rev);
  const { items: jobItems } = useStore(jobs);
  const [version, setVersion] = useState(0);
  const [filter, setFilter] = useState(null); // a library folder, OTHER, or null for all
  const [limit, setLimit] = useState(PAGE);
  const [selected, setSelected] = useState(() => new Set());
  const refresh = () => setVersion((v) => v + 1);
  const { data: summary, error } = useAsync(() => api('/tools/missing/summary'), [rev, version]);
  const { data: list } = useAsync(() => api('/tools/missing/items', { params: { limit, root: filter } }), [rev, version, limit, filter]);
  const scanning = jobItems.some((j) => j.kind === 'scan' && (j.status === 'running' || j.status === 'queued'));
  useEffect(() => setSelected(new Set()), [rev, filter]);

  async function checkAgain() {
    try {
      const { job } = await api('/library/scan', { method: 'POST' });
      trackJob(job);
      toast('Looking for those files again. Songs whose files are back return on their own.');
    } catch (err) {
      notifyError(err);
    }
  }

  async function forget(body, label, detail) {
    const ok = await confirmDialog({
      title: `Forget ${label}?`,
      message: `${detail} Their playlist entries, rating and favorite go with them. Your listening history stays. The files themselves are not touched; if they come back, a scan adds them as new songs.`,
      confirm: `Forget ${label}`,
      danger: true,
    });
    if (!ok) return;
    try {
      const outcome = await api('/tools/missing/forget', { method: 'POST', body: { ...body, confirm: true } });
      toast(`Forgot ${fmt.plural(outcome.forgotten, 'song')}.${outcome.kept ? ` ${fmt.plural(outcome.kept, 'song')} had turned up again and ${outcome.kept === 1 ? 'was' : 'were'} kept.` : ''}`, 'success');
      setSelected(new Set());
      refresh();
      bumpLibrary();
      loadPlaylists().catch(() => {});
    } catch (err) {
      notifyError(err);
    }
  }

  if (error) return html`<${PageHeader} title="Missing files" /><p class="subtle">${error.message}</p>`;
  if (!summary) return html`<${PageHeader} title="Missing files" /><${Spinner} />`;

  const items = list?.items || [];
  const toggle = (id) => setSelected((s) => { const next = new Set(s); next.has(id) ? next.delete(id) : next.add(id); return next; });
  const allShown = items.length > 0 && items.every((i) => selected.has(i.id));
  const showing = filter === null ? 'all folders' : filter === OTHER ? 'folders no longer in your library' : shorten(filter);

  return html`
    <${PageHeader} title="Missing files" subtitle="Songs your library lists but whose files cannot be found, usually because a drive is not plugged in.">
      <${Button} icon="refresh" disabled=${scanning} onClick=${checkAgain}>Check again<//>
    <//>
    <${JobBanner} kinds=${['scan']} />
    ${summary.total === 0
      ? html`<${Empty} icon="check" title="Nothing is missing"><p>Every song in your library has its file. If you move or delete files outside the app, they show up here after the next scan.</p><//>`
      : html`
        <div class="callout"><p><${Icon} name="info" size=${16} /><span>A song stays in your library, with its plays, rating and playlists, while its file is away, so a drive that is unplugged costs you nothing. Press “Check again” once it is connected. Forget songs only when you know the files are gone for good.</span></p></div>
        <div class="stat-row">
          <div class="stat"><strong>${fmt.number(summary.total)}</strong><span>${summary.total === 1 ? 'song' : 'songs'} cannot be found</span></div>
        </div>
        <div class="folder-grid">
          ${summary.roots.filter((r) => r.count > 0).map((r) => html`<${FolderCard} key=${r.path} title=${r.path} count=${r.count} available=${r.available} active=${filter === r.path} busy=${scanning}
            note=${r.available ? 'the folder is there, so these files were moved or deleted' : 'plug the drive in and check again'}
            onShow=${() => setFilter(filter === r.path ? null : r.path)}
            onForget=${() => forget({ root: r.path }, fmt.plural(r.count, 'song'), `These ${r.count === 1 ? 'song is' : 'songs are'} from ${r.path}.${r.available ? '' : ' That folder is not connected right now, so the files may only be out of reach.'}`)} />`)}
          ${summary.elsewhere > 0 && html`<${FolderCard} title="Folders no longer in your library" count=${summary.elsewhere} active=${filter === OTHER} busy=${scanning}
            note="from folders you removed"
            onShow=${() => setFilter(filter === OTHER ? null : OTHER)}
            onForget=${() => forget({ root: OTHER }, fmt.plural(summary.elsewhere, 'song'), 'These songs belong to folders you took out of your library. Adding a folder back brings its songs back, so forget them only if you do not plan to.')} />`}
        </div>
        <div class="enrich-bar">
          <label class="check"><input type="checkbox" checked=${allShown} onChange=${() => setSelected(allShown ? new Set() : new Set(items.map((i) => i.id)))} /> ${selected.size ? `${fmt.number(selected.size)} selected` : 'Select shown'}</label>
          <span class="subtle">Showing ${fmt.number(list?.total ?? 0)} from ${showing}</span>
          <span class="spacer"></span>
          <${Button} small kind="danger-ghost" icon="trash" disabled=${scanning || !selected.size} onClick=${() => forget({ track_ids: [...selected] }, fmt.plural(selected.size, 'song'), '')}>Forget selected…<//>
          <${Button} small kind="danger-ghost" icon="trash" disabled=${scanning} onClick=${() => forget({ everything: true }, `all ${fmt.plural(summary.total, 'song')}`, 'Every song whose file cannot be found will be removed.')}>Forget all…<//>
        </div>
        <div class="review-table" role="table" aria-label="Missing songs">
          <div class="review-head" role="row"><span></span><span>Song</span><span>Where it was</span><span>Plays</span></div>
          ${items.map((item) => html`<label class="review-row ${selected.has(item.id) ? 'selected' : ''}" role="row" key=${item.id}>
            <input type="checkbox" checked=${selected.has(item.id)} onChange=${() => toggle(item.id)} aria-label=${`Select ${item.title || item.path}`} />
            <span class="cell"><strong>${item.title || 'Untitled'}</strong><span class="subtle">${[item.artist, item.album].filter(Boolean).join(' · ')}</span></span>
            <span class="cell"><span class="path" title=${item.path}>${shorten(item.path)}</span>${item.last_seen && html`<span class="subtle">last seen ${fmt.date(item.last_seen)}</span>`}</span>
            <span class="cell">${item.plays || ''}</span>
          </label>`)}
        </div>
        ${list && list.total > items.length && html`<p class="more-row"><${Button} onClick=${() => setLimit(limit + PAGE)}>Show more (${fmt.number(list.total - items.length)} left)<//></p>`}`}`;
}
