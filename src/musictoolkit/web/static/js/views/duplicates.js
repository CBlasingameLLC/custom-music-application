// Duplicate songs: find them, pick the copy to keep, move the rest to a review folder; and the review folder itself.
import { api, fmt, html, useAsync, useEffect, useRef, useState, useStore } from '../lib.js';
import { Icon } from '../icons.js';
import { Button, Empty, JobBanner, PageHeader, Spinner } from '../components.js';
import { addMusicFolder, confirmDialog, typedConfirm } from '../dialogs.js';
import { jobs, library, notifyError, onJobFinished, toast, trackJob } from '../state.js';

const KINDS = ['dedupe-scan', 'dedupe', 'dedupe-restore', 'dedupe-purge'];
const PAGE = 25;
const REVIEW_PAGE = 100;

const when = (iso) => {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : `${date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}, ${date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}`;
};

/** The last few folders and the file name: enough to tell copies apart without a 200-character line. */
const shorten = (path) => {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.length > 3 ? `…/${parts.slice(-3).join('/')}` : parts.join('/');
};

function Member({ member, group, keeper, onKeep }) {
  const keeping = member.id === keeper;
  return html`<label class="dup-member ${keeping ? 'keep' : 'move'}">
    <input type="radio" name=${'keep-' + group.key} checked=${keeping} onChange=${() => onKeep(member.id)} aria-label=${`Keep ${member.path}`} />
    <span class="dup-main">
      <span class="dup-path" title=${member.path}>${shorten(member.path)}</span>
      <span class="dup-meta">
        <span class="chip">${(member.format || '?').toUpperCase()}${member.bitrate ? ` · ${Math.round(member.bitrate / 1000)} kbps` : ''}</span>
        <span>${fmt.bytes(member.size)}</span>
        <span>${fmt.time(member.duration)}</span>
        ${member.album && html`<span>${member.album}${member.year ? ` (${member.year})` : ''}</span>`}
        ${member.plays > 0 && html`<span>${fmt.plural(member.plays, 'play')}</span>`}
        ${member.rating > 0 && html`<span aria-label=${`${member.rating} stars`}>${'★'.repeat(member.rating)}</span>`}
        ${member.favorite && html`<span>♥ favorite</span>`}
      </span>
    </span>
    <span class="dup-verdict">${keeping ? (member.id === group.keeper ? 'Keep · suggested' : 'Keep') : 'Move to review folder'}</span>
  </label>`;
}

function GroupCard({ group, keeper, included, busy, onKeep, onInclude, onDifferent }) {
  const first = group.members[0];
  return html`<section class="dup-group ${included ? '' : 'off'}">
    <header class="dup-head">
      <input type="checkbox" checked=${included} onChange=${(e) => onInclude(e.target.checked)} aria-label=${`Include ${first.title || 'this song'} in the move`} />
      <strong class="dup-title">${first.title || 'Untitled'}</strong>
      <span class="subtle">${first.artist || ''}</span>
      <span class="chip ${group.identical ? 'good' : 'ok'}">${group.label}</span>
      <span class="spacer"></span>
      <button class="link" disabled=${busy} onClick=${onDifferent}>These are different songs</button>
    </header>
    ${group.members.map((m) => html`<${Member} key=${m.id} member=${m} group=${group} keeper=${keeper} onKeep=${onKeep} />`)}
  </section>`;
}

// ------------------------------------------------------------ find

function FindTab({ info, busy, refresh }) {
  const [exact, setExact] = useState(false);
  const [scan, setScan] = useState(null); // { jobId, result, items, total }
  const [pending, setPending] = useState(null);
  const [picks, setPicks] = useState({}); // group key -> the id to keep
  const [excluded, setExcluded] = useState(() => new Set());
  const [loading, setLoading] = useState(false);
  const [problems, setProblems] = useState(null); // copies the last move could not handle
  const pendingRef = useRef(null);
  const exactRef = useRef(false);
  pendingRef.current = pending;
  exactRef.current = exact;

  async function loadGroups(job, offset = 0) {
    try {
      const page = await api(`/tools/duplicates/groups/${job.id}`, { params: { offset, limit: PAGE } });
      setScan((prev) => (offset === 0 || !prev ? { jobId: job.id, result: job.result, items: page.items, total: page.total } : { ...prev, items: [...prev.items, ...page.items], total: page.total }));
    } catch (error) {
      notifyError(error);
    }
  }

  async function startScan() {
    setScan(null);
    setPicks({});
    setExcluded(new Set());
    try {
      const { job } = await api('/tools/duplicates/scan', { method: 'POST', body: { exact: exactRef.current } });
      pendingRef.current = job.id; // before the job can finish, not after the next render
      setPending(job.id);
      trackJob(job);
    } catch (error) {
      notifyError(error);
    }
  }

  useEffect(() => {
    if (info.scan) loadGroups({ id: info.scan.job_id, result: info.scan }, 0); // pick up where the last visit left off
  }, []);

  useEffect(() => onJobFinished((job) => {
    if (job.kind === 'dedupe-scan' && job.id === pendingRef.current) {
      setPending(null);
      if (job.status === 'done') loadGroups(job, 0);
    } else if (job.kind === 'dedupe') {
      refresh();
      setScan(null);
      const r = job.result;
      if (job.status === 'done' && r) {
        toast(`Moved ${fmt.plural(r.moved, 'copy', 'copies')} to the review folder.${r.skipped ? ` ${fmt.plural(r.skipped, 'copy', 'copies')} could not be moved.` : ''}`,
          r.skipped ? 'info' : 'success', 12000, r.moved ? { label: 'Undo', onClick: () => undoBatch(r.batch_id) } : null);
        if (r.errors_total) setProblems({ title: 'These copies were left where they were', errors: r.errors, total: r.errors_total });
        startScan(); // show what is left
      } else if (job.status === 'cancelled') {
        toast('Stopped. Copies already moved are in the review folder, where you can restore them.', 'info', 10000);
      }
    } else if (job.kind === 'dedupe-restore' || job.kind === 'dedupe-purge') {
      refresh();
      const r = job.result;
      if (job.status === 'done' && r) {
        const verb = job.kind === 'dedupe-restore' ? 'Restored' : 'Moved';
        const tail = job.kind === 'dedupe-restore' ? '.' : ' to the Recycle Bin.';
        toast(`${verb} ${fmt.plural(r.moved, 'song')}${tail}${r.skipped ? ` ${fmt.plural(r.skipped, 'song')} could not be ${job.kind === 'dedupe-restore' ? 'restored' : 'deleted'}.` : ''}`, r.skipped ? 'info' : 'success', 8000);
        if (r.errors_total) setProblems({ title: job.kind === 'dedupe-restore' ? 'These songs could not be restored' : 'These songs could not be deleted', errors: r.errors, total: r.errors_total });
      }
    }
  }), []);

  async function undoBatch(batchId) {
    try {
      const { job } = await api('/tools/duplicates/undo', { method: 'POST', body: { batch_id: batchId } });
      trackJob(job);
    } catch (error) {
      notifyError(error);
    }
  }

  async function notDuplicates(group) {
    try {
      await api('/tools/duplicates/ignore', { method: 'POST', body: { keys: [group.key] } });
      setScan((s) => s && { ...s, items: s.items.filter((g) => g.key !== group.key), total: s.total - 1 });
      refresh();
    } catch (error) {
      notifyError(error);
    }
  }

  async function showMore(all) {
    setLoading(true);
    try {
      let have = scan.items.length;
      const total = scan.total;
      while (have < total) {
        const page = await api(`/tools/duplicates/groups/${scan.jobId}`, { params: { offset: have, limit: all ? 100 : PAGE } });
        if (!page.items.length) break;
        have += page.items.length;
        setScan((prev) => prev && { ...prev, items: [...prev.items, ...page.items], total: page.total });
        if (!all) break;
      }
    } catch (error) {
      notifyError(error);
    } finally {
      setLoading(false);
    }
  }

  const keeperOf = (g) => picks[g.key] ?? g.keeper;
  const chosen = (scan?.items || []).filter((g) => !excluded.has(g.key));
  const copies = chosen.reduce((n, g) => n + g.members.length - 1, 0);
  const freed = chosen.reduce((n, g) => n + g.members.filter((m) => m.id !== keeperOf(g)).reduce((s, m) => s + (m.size || 0), 0), 0);

  async function moveThem() {
    const ok = await confirmDialog({
      title: `Move ${fmt.plural(copies, 'copy', 'copies')} to the review folder?`,
      message: `Each copy goes to a “_duplicates_review” folder inside its library folder and disappears from your library. Its plays, playlist entries, rating and favorite are handed to the copy you keep. Nothing is deleted, and you can undo this.`,
      confirm: `Move ${fmt.plural(copies, 'copy', 'copies')}`,
    });
    if (!ok) return;
    try {
      const choices = chosen.map((g) => ({ key: g.key, keeper: keeperOf(g), remove: g.members.filter((m) => m.id !== keeperOf(g)).map((m) => m.id) }));
      const { job } = await api('/tools/duplicates/quarantine', { method: 'POST', body: { job_id: scan.jobId, choices } });
      trackJob(job);
    } catch (error) {
      notifyError(error);
    }
  }

  const result = scan?.result;
  return html`
    <div class="row-actions">
      <${Button} kind="primary" icon="search" disabled=${busy || !info.has_folders} onClick=${startScan}>${scan || pending ? 'Search again' : 'Find duplicates'}<//>
      <label class="check" title="Reads every file that has the same size as another, so it can take a while, but finds exact copies whatever their tags say.">
        <input type="checkbox" checked=${exact} disabled=${busy} onChange=${(e) => setExact(e.target.checked)} /> Also compare file contents (slower)
      </label>
      ${info.ignored > 0 && html`<button class="link" disabled=${busy} onClick=${async () => { await api('/tools/duplicates/ignore/reset', { method: 'POST' }).catch(notifyError); toast('Songs you marked as different will be checked again.'); refresh(); }}>Check the ${fmt.number(info.ignored)} I marked as different again</button>`}
    </div>
    ${!info.has_folders && html`<${Empty} icon="folder" title="No music folder yet"><p>Add the folder your music lives in first.</p><${Button} kind="primary" icon="plus" onClick=${addMusicFolder}>Add music folder<//><//>`}
    ${problems && html`<div class="callout warn" role="alert">
      <p><${Icon} name="alert" size=${16} /><span>${problems.title}${problems.total > problems.errors.length ? ` (showing ${problems.errors.length} of ${fmt.number(problems.total)})` : ''}</span></p>
      <ul class="problem-list">${problems.errors.slice(0, 20).map((e) => html`<li key=${e.path}><code>${e.path}</code> <span class="subtle">${e.error}</span></li>`)}</ul>
      <p><button class="link" onClick=${() => setProblems(null)}>Dismiss</button></p>
    </div>`}
    ${result && html`
      <div class="stat-row">
        <div class="stat"><strong>${fmt.number(result.groups)}</strong><span>${result.groups === 1 ? 'song' : 'songs'} found more than once</span></div>
        <div class="stat"><strong>${fmt.number(result.copies)}</strong><span>extra ${result.copies === 1 ? 'copy' : 'copies'}</span></div>
        <div class="stat"><strong>${fmt.bytes(result.bytes)}</strong><span>could be freed</span></div>
      </div>
      ${result.groups === 0 && html`<${Empty} icon="check" title="No duplicates found">
        <p>${result.exact ? 'No two songs match by tags or by content.' : 'No two songs share a recording ID, or an artist, title and length.'}${result.ignored ? ` ${fmt.plural(result.ignored, 'match', 'matches')} you marked as different ${result.ignored === 1 ? 'was' : 'were'} left out.` : ''}</p>
        ${!result.exact && html`<p>Tick “Also compare file contents” to find exact copies whose tags differ.</p>`}
      <//>`}`}
    ${scan && scan.items.length > 0 && html`
      <div class="enrich-bar dup-bar">
        <span><strong>${fmt.plural(copies, 'copy', 'copies')}</strong> in ${fmt.plural(chosen.length, 'group')} will move${freed ? ` · frees ${fmt.bytes(freed)}` : ''}</span>
        <span class="spacer"></span>
        <${Button} kind="primary" icon="check" disabled=${busy || !copies} onClick=${moveThem}>Move to review folder<//>
      </div>
      <p class="subtle dup-help">Pick the copy to keep in each group. The best quality is suggested. Untick a group to leave it alone this time.</p>
      ${scan.items.map((group) => html`<${GroupCard} key=${group.key} group=${group} keeper=${keeperOf(group)} busy=${busy} included=${!excluded.has(group.key)}
        onKeep=${(id) => setPicks((p) => ({ ...p, [group.key]: id }))}
        onInclude=${(on) => setExcluded((s) => { const next = new Set(s); on ? next.delete(group.key) : next.add(group.key); return next; })}
        onDifferent=${() => notDuplicates(group)} />`)}
      ${scan.total > scan.items.length && html`<p class="more-row">
        <${Button} disabled=${loading} onClick=${() => showMore(false)}>Show more (${fmt.number(scan.total - scan.items.length)} left)<//>
        <button class="link" disabled=${loading} onClick=${() => showMore(true)}>Show all</button>
      </p>`}`}
    ${!scan && !pending && !result && info.has_folders && html`<p class="subtle">Songs count as the same when they share a MusicBrainz recording ID, or have the same artist, title and length. You choose what stays; nothing is deleted.</p>`}`;
}

// ------------------------------------------------------------ review folder

function ReviewTab({ info, busy, refresh }) {
  const rev = useStore(library, (s) => s.rev);
  const [limit, setLimit] = useState(REVIEW_PAGE);
  const [selected, setSelected] = useState(() => new Set());
  const { data } = useAsync(() => api('/tools/duplicates/review', { params: { limit } }), [rev, limit, info.review.count]);
  useEffect(() => setSelected(new Set()), [rev]);
  const items = data?.items || [];
  const toggle = (id) => setSelected((s) => { const next = new Set(s); next.has(id) ? next.delete(id) : next.add(id); return next; });
  const allShown = items.length > 0 && items.every((i) => selected.has(i.id));

  async function restore(body) {
    try {
      const { job } = await api('/tools/duplicates/restore', { method: 'POST', body });
      trackJob(job);
      setSelected(new Set());
    } catch (error) {
      notifyError(error);
    }
  }

  async function remove(body, label) {
    const ok = await typedConfirm({
      title: `Delete ${label}?`,
      message: 'They are moved to the Recycle Bin, so you can still get them back from there. They are removed from your library and from any playlist that listed them.',
      confirm: `Delete ${label}`,
    });
    if (!ok) return;
    try {
      const { job } = await api('/tools/duplicates/delete', { method: 'POST', body: { ...body, confirm: 'DELETE' } });
      trackJob(job);
      setSelected(new Set());
    } catch (error) {
      notifyError(error);
    }
  }

  async function undoBatch(batch) {
    try {
      const { job } = await api('/tools/duplicates/undo', { method: 'POST', body: { batch_id: batch.batch_id } });
      trackJob(job);
    } catch (error) {
      notifyError(error);
    }
  }

  if (!data) return html`<${Spinner} />`;
  return html`
    ${data.count === 0
      ? html`<${Empty} icon="check" title="The review folder is empty"><p>Copies you move out of the library wait here. Restore them, or delete them to the Recycle Bin once you are sure.</p><//>`
      : html`
        <div class="stat-row">
          <div class="stat"><strong>${fmt.number(data.count)}</strong><span>${data.count === 1 ? 'copy' : 'copies'} waiting</span></div>
          <div class="stat"><strong>${fmt.bytes(data.bytes)}</strong><span>on disk</span></div>
        </div>
        <div class="enrich-bar">
          <label class="check"><input type="checkbox" checked=${allShown} onChange=${() => setSelected(allShown ? new Set() : new Set(items.map((i) => i.id)))} /> ${selected.size ? `${fmt.number(selected.size)} selected` : 'Select shown'}</label>
          <span class="spacer"></span>
          <${Button} small icon="undo" disabled=${busy || !selected.size} onClick=${() => restore({ track_ids: [...selected] })}>Restore selected<//>
          <${Button} small icon="trash" kind="danger-ghost" disabled=${busy || !selected.size} onClick=${() => remove({ track_ids: [...selected] }, fmt.plural(selected.size, 'song'))}>Delete selected…<//>
          <${Button} small icon="undo" disabled=${busy} onClick=${() => restore({ everything: true })}>Restore all<//>
          <${Button} small icon="trash" kind="danger-ghost" disabled=${busy} onClick=${() => remove({ everything: true }, `all ${fmt.plural(data.count, 'song')}`)}>Delete all…<//>
        </div>
        <div class="review-table" role="table" aria-label="Copies in the review folder">
          <div class="review-head" role="row"><span></span><span>Song</span><span>Came from</span><span>Size</span></div>
          ${items.map((item) => html`<label class="review-row ${selected.has(item.id) ? 'selected' : ''}" role="row" key=${item.id}>
            <input type="checkbox" checked=${selected.has(item.id)} onChange=${() => toggle(item.id)} aria-label=${`Select ${item.title || item.path}`} />
            <span class="cell"><strong>${item.title || 'Untitled'}</strong><span class="subtle">${[item.artist, item.album].filter(Boolean).join(' · ')}</span></span>
            <span class="cell"><span class="path" title=${item.original || item.path}>${shorten(item.original || item.path)}</span>${item.moved_at && html`<span class="subtle">${when(item.moved_at)}</span>`}</span>
            <span class="cell">${fmt.bytes(item.size)}${item.exists ? '' : html`<span class="subtle">file is gone</span>`}</span>
          </label>`)}
        </div>
        ${data.total > items.length && html`<p class="more-row"><${Button} onClick=${() => setLimit(limit + REVIEW_PAGE)}>Show more (${fmt.number(data.total - items.length)} left)<//></p>`}`}
    ${info.batches.length > 0 && html`<h2 class="section-title">Recent moves</h2>
      <div class="batch-list">
        ${info.batches.map((batch) => html`<div class="batch-row" key=${batch.batch_id}>
          <span class="batch-when">${when(batch.moved_at)}</span>
          <span>${fmt.plural(batch.files, 'copy', 'copies')} moved</span>
          <span class="spacer"></span>
          ${batch.undoable
            ? html`<${Button} small icon="undo" disabled=${busy} onClick=${() => undoBatch(batch)}>Undo${batch.undoable < batch.files ? ` (${fmt.number(batch.undoable)} left)` : ''}<//>`
            : html`<span class="subtle">Undone</span>`}
        </div>`)}
      </div>`}
    <p class="hint-card subtle"><${Icon} name="info" size=${14} /> Scans skip the “_duplicates_review” folders, so nothing in them shows up in your library. Deleting sends files to the Recycle Bin; restoring puts them back where they were.</p>`;
}

// ------------------------------------------------------------ page

export function DuplicatesView() {
  const rev = useStore(library, (s) => s.rev);
  const { items: jobItems } = useStore(jobs);
  const [version, setVersion] = useState(0);
  const [tab, setTab] = useState('find');
  const refresh = () => setVersion((v) => v + 1);
  const { data: info, error } = useAsync(() => api('/tools/duplicates/info'), [rev, version]);
  const busy = jobItems.some((j) => KINDS.includes(j.kind) && (j.status === 'running' || j.status === 'queued'));

  if (error) return html`<${PageHeader} title="Find duplicates" /><p class="subtle">${error.message}</p>`;
  if (!info) return html`<${PageHeader} title="Find duplicates" /><${Spinner} />`;
  return html`
    <${PageHeader} title="Find duplicates" subtitle="Find songs you have more than once, keep the best copy, and move the rest to a review folder. Nothing is deleted until you empty that folder, and even then it goes to the Recycle Bin." />
    <div class="tabs" role="tablist">
      <button role="tab" aria-selected=${tab === 'find'} class="tab ${tab === 'find' ? 'active' : ''}" onClick=${() => setTab('find')}>Find</button>
      <button role="tab" aria-selected=${tab === 'review'} class="tab ${tab === 'review' ? 'active' : ''}" onClick=${() => setTab('review')}>Review folder${info.review.count ? html` <span class="count">${fmt.number(info.review.count)}</span>` : ''}</button>
    </div>
    <${JobBanner} kinds=${KINDS} />
    <div hidden=${tab !== 'find'}><${FindTab} info=${info} busy=${busy} refresh=${refresh} /></div>
    ${tab === 'review' && html`<${ReviewTab} info=${info} busy=${busy} refresh=${refresh} />`}`;
}
