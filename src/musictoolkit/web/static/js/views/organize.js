// Organize files: choose a folder and a layout, preview every change, move the files as one batch, undo it.
import { api, fmt, html, useAsync, useDebounced, useEffect, useRef, useState, useStore } from '../lib.js';
import { Icon } from '../icons.js';
import { Button, Empty, JobBanner, PageHeader, Spinner } from '../components.js';
import { addMusicFolder, confirmDialog } from '../dialogs.js';
import { href, jobs, library, notifyError, onJobFinished, toast, trackJob } from '../state.js';

const KINDS = ['organize-preview', 'organize', 'organize-undo'];
const PAGE = 100;
const LISTS = [
  ['moves', 'Will move'],
  ['collisions', 'Name already taken'],
  ['too_long', 'Path too long'],
];

const when = (iso) => {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : `${date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}, ${date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}`;
};

function Examples({ example }) {
  if (!example) return html`<div class="example-box"> </div>`;
  if (!example.ok) return html`<div class="example-box bad" role="alert"><${Icon} name="alert" size=${15} /><span>${example.error}</span></div>`;
  return html`<div class="example-box">
    <span class="subtle">For example</span>
    ${example.examples.map((line) => html`<code key=${line}>${line}</code>`)}
  </div>`;
}

function Batches({ batches, busy, onUndo }) {
  if (!batches.length) return null;
  return html`<h2 class="section-title">Recent changes</h2>
    <div class="batch-list">
      ${batches.map((batch) => html`<div class="batch-row" key=${batch.batch_id}>
        <span class="batch-when">${when(batch.moved_at)}</span>
        <span>${fmt.plural(batch.files, 'song')} moved</span>
        <span class="spacer"></span>
        ${batch.undoable
          ? html`<${Button} small icon="undo" disabled=${busy} onClick=${() => onUndo(batch)}>Undo${batch.undoable < batch.files ? ` (${fmt.number(batch.undoable)} left)` : ''}<//>`
          : html`<span class="subtle">Undone</span>`}
      </div>`)}
    </div>`;
}

export function OrganizeView() {
  const rev = useStore(library, (s) => s.rev);
  const { items: jobItems } = useStore(jobs);
  const [version, setVersion] = useState(0);
  const { data: info, error } = useAsync(() => api('/tools/organize/info'), [rev, version]);
  const [root, setRoot] = useState('');
  const [scheme, setScheme] = useState(() => library.get().settings?.library?.canonical_scheme || '');
  const [plan, setPlan] = useState(null); // { jobId, root, scheme, result, lists: { moves: {items,total}, ... } }
  const [list, setList] = useState('moves');
  const [pending, setPending] = useState(null); // id of the preview job we are waiting for
  const [problems, setProblems] = useState(null); // songs the last batch could not move
  const refresh = () => setVersion((v) => v + 1);
  const pendingRef = useRef(null);
  pendingRef.current = pending;
  const typed = useDebounced(scheme, 250);
  const { data: example } = useAsync(
    () => (typed.trim() ? api('/tools/organize/example', { params: { scheme: typed } }) : null),
    [typed],
  );

  const busy = jobItems.some((j) => KINDS.includes(j.kind) && (j.status === 'running' || j.status === 'queued'));

  useEffect(() => {
    if (!info) return;
    setRoot((current) => (info.roots.some((r) => r.path === current) ? current : info.roots.find((r) => r.available)?.path || info.roots[0]?.path || ''));
    setScheme((current) => current || info.scheme);
  }, [info]);

  useEffect(() => onJobFinished((job) => {
    if (job.kind === 'organize-preview' && job.id === pendingRef.current) {
      setPending(null);
      if (job.status === 'done') loadPlan(job);
    } else if (job.kind === 'organize') {
      refresh();
      setPlan(null);
      const r = job.result;
      if (job.status === 'done' && r) {
        const note = r.skipped ? ` ${fmt.plural(r.skipped, 'song')} could not be moved.` : '';
        toast(`Moved ${fmt.plural(r.moved, 'song')}.${note}`, r.skipped ? 'info' : 'success', 12000, r.moved ? { label: 'Undo', onClick: () => undo({ batch_id: r.batch_id }) } : null);
        if (r.errors_total) setProblems({ title: 'These songs were left where they were', errors: r.errors, total: r.errors_total });
      } else if (job.status === 'cancelled') {
        toast('Stopped. Songs already moved are listed under Recent changes, where you can undo them.', 'info', 10000);
      }
    } else if (job.kind === 'organize-undo') {
      refresh();
      const r = job.result;
      if (job.status === 'done' && r) {
        toast(`Put ${fmt.plural(r.moved, 'song')} back.${r.skipped ? ` ${fmt.plural(r.skipped, 'song')} could not be restored.` : ''}`, r.skipped ? 'info' : 'success', 8000);
        if (r.errors_total) setProblems({ title: 'These songs could not be put back', errors: r.errors, total: r.errors_total });
      }
    }
  }), []);

  async function loadPlan(job) {
    try {
      const result = job.result;
      const moves = await api(`/tools/organize/preview/${job.id}`, { params: { kind: 'moves', limit: PAGE } });
      setPlan({ jobId: job.id, root: result.root, scheme: result.scheme, result, lists: { moves } });
      setList('moves');
    } catch (err) {
      notifyError(err);
    }
  }

  async function showList(kind) {
    setList(kind);
    if (!plan || plan.lists[kind]) return;
    try {
      const page = await api(`/tools/organize/preview/${plan.jobId}`, { params: { kind, limit: PAGE } });
      setPlan((p) => p && { ...p, lists: { ...p.lists, [kind]: page } });
    } catch (err) {
      notifyError(err);
    }
  }

  async function more() {
    const have = plan.lists[list];
    try {
      const page = await api(`/tools/organize/preview/${plan.jobId}`, { params: { kind: list, offset: have.items.length, limit: PAGE } });
      setPlan((p) => p && { ...p, lists: { ...p.lists, [list]: { ...page, items: [...have.items, ...page.items] } } });
    } catch (err) {
      notifyError(err);
    }
  }

  async function runPreview() {
    setProblems(null);
    setPlan(null);
    try {
      const { job } = await api('/tools/organize/preview', { method: 'POST', body: { root, scheme } });
      pendingRef.current = job.id; // before the job can finish, not after the next render
      setPending(job.id);
      trackJob(job);
    } catch (err) {
      notifyError(err);
    }
  }

  async function apply() {
    const count = plan.result.moves;
    const ok = await confirmDialog({
      title: `Move ${fmt.plural(count, 'song')}?`,
      message: `Their files will be moved and renamed on disk, together with lyrics files and cover pictures. Nothing is deleted or overwritten, and you can undo the whole batch afterwards from “Recent changes”.`,
      confirm: `Move ${fmt.plural(count, 'song')}`,
    });
    if (!ok) return;
    try {
      const { job } = await api('/tools/organize/apply', { method: 'POST', body: { root: plan.root, scheme: plan.scheme } });
      trackJob(job);
    } catch (err) {
      notifyError(err);
    }
  }

  async function undo(batch) {
    try {
      const { job } = await api('/tools/organize/undo', { method: 'POST', body: { batch_id: batch.batch_id } });
      trackJob(job);
    } catch (err) {
      notifyError(err);
    }
  }

  const insert = (name) => setScheme((s) => s + `{${name}}`);
  if (error) return html`<${PageHeader} title="Organize files" /><p class="subtle">${error.message}</p>`;
  if (!info) return html`<${PageHeader} title="Organize files" /><${Spinner} />`;

  const current = plan && plan.root === root && plan.scheme === scheme.trim();
  const stale = plan && !current;
  const result = plan?.result;
  const shown = plan?.lists[list];
  const selectedRoot = info.roots.find((r) => r.path === root);
  const preset = info.presets.find((p) => p.scheme === scheme)?.scheme || '';

  return html`
    <${PageHeader} title="Organize files" subtitle="Move and rename your music into a tidy folder layout built from its tags. You see every change first, and the whole batch can be undone." />
    ${!info.roots.length
      ? html`<${Empty} icon="folder" title="No music folder yet">
          <p>Add the folder your music lives in, then come back to tidy it.</p>
          <${Button} kind="primary" icon="plus" onClick=${addMusicFolder}>Add music folder<//>
        <//>`
      : html`
      <div class="organize-form">
        <label class="field">
          <span>Folder to organize</span>
          <select aria-label="Folder to organize" value=${root} disabled=${busy} onChange=${(e) => setRoot(e.target.value)}>
            ${info.roots.map((r) => html`<option value=${r.path} key=${r.path}>${r.path} · ${fmt.plural(r.songs, 'song')}${r.available ? '' : ' (not connected)'}</option>`)}
          </select>
        </label>
        <label class="field">
          <span>Layout</span>
          <select aria-label="Layout preset" value=${preset} disabled=${busy} onChange=${(e) => e.target.value && setScheme(e.target.value)}>
            <option value="">Custom</option>
            ${info.presets.map((p) => html`<option value=${p.scheme} key=${p.scheme}>${p.name}</option>`)}
          </select>
        </label>
        <label class="field wide">
          <span>Folder and file name, built from these fields</span>
          <input class="mono" type="text" spellcheck="false" value=${scheme} disabled=${busy} onInput=${(e) => setScheme(e.target.value)} />
        </label>
        <div class="field-chips" aria-label="Insert a field">
          ${info.fields.map((f) => html`<button class="chip-btn" type="button" key=${f.name} title=${f.help} disabled=${busy} onClick=${() => insert(f.name)}>{${f.name}}</button>`)}
        </div>
        <${Examples} example=${example} />
        <div class="row-actions">
          <${Button} kind="primary" icon="search" disabled=${busy || !selectedRoot?.available || !scheme.trim() || example?.ok === false} onClick=${runPreview}>Preview changes<//>
          ${selectedRoot && !selectedRoot.available && html`<span class="note warn"><${Icon} name="alert" size=${14} /> That folder is not connected right now.</span>`}
        </div>
      </div>
      <${JobBanner} kinds=${KINDS} />

      ${problems && html`<div class="callout warn" role="alert">
        <p><${Icon} name="alert" size=${16} /><span>${problems.title}${problems.total > problems.errors.length ? ` (showing ${problems.errors.length} of ${fmt.number(problems.total)})` : ''}</span></p>
        <ul class="problem-list">${problems.errors.slice(0, 20).map((e) => html`<li key=${e.path}><code>${e.path}</code> <span class="subtle">${e.error}</span></li>`)}</ul>
        <p><button class="link" onClick=${() => setProblems(null)}>Dismiss</button></p>
      </div>`}

      ${stale && html`<div class="callout"><p><${Icon} name="info" size=${16} /><span>You changed the folder or layout after previewing. Preview again to see what would happen now.</span></p></div>`}

      ${current && result && html`
        <div class="stat-row">
          <div class="stat"><strong>${fmt.number(result.moves)}</strong><span>${result.moves === 1 ? 'song' : 'songs'} will move</span></div>
          <div class="stat"><strong>${fmt.number(result.unchanged)}</strong><span>already in place</span></div>
          <div class="stat"><strong>${fmt.number(result.collisions)}</strong><span>left alone (name taken)</span></div>
          ${result.too_long > 0 && html`<div class="stat"><strong>${fmt.number(result.too_long)}</strong><span>left alone (path too long)</span></div>`}
        </div>
        ${result.unknown > 0 && html`<div class="callout warn"><p><${Icon} name="alert" size=${16} /><span>${fmt.plural(result.unknown, 'song')} ${result.unknown === 1 ? 'is' : 'are'} missing a title, artist or album and would land under “Unknown Artist” or “Unknown Album”. <a href=${href('/tools/enrich')}>Fix missing tags first</a> for a better result.</span></p></div>`}
        ${result.moves === 0
          ? html`<${Empty} icon="check" title=${result.collisions ? 'Nothing can move yet' : 'Everything is already organized'}>
              <p>${result.collisions ? 'Every song that would move wants a name that is already taken. Look at the list below, then adjust the layout.' : 'Every song in this folder is already where this layout puts it.'}</p>
            <//>`
          : html`<div class="organize-apply">
              <${Button} kind="primary" icon="check" disabled=${busy} onClick=${apply}>Move ${fmt.plural(result.moves, 'song')}<//>
              <span class="subtle">Lyrics files and covers follow. Folders left empty are removed.</span>
            </div>`}
        <div class="tabs" role="tablist">
          ${LISTS.filter(([kind]) => kind === 'moves' || result[kind] > 0).map(([kind, label]) => html`<button role="tab" key=${kind} aria-selected=${list === kind} class="tab ${list === kind ? 'active' : ''}" onClick=${() => showList(kind)}>${label} <span class="count">${fmt.number(kind === 'moves' ? result.moves : result[kind])}</span></button>`)}
        </div>
        ${shown && html`<div class="move-table" role="table" aria-label=${LISTS.find(([k]) => k === list)[1]}>
          <div class="move-head" role="row"><span>Now</span><span></span><span>${list === 'moves' ? 'After' : 'Wanted'}</span></div>
          ${shown.items.map((item, i) => html`<div class="move-row" role="row" key=${i + item.from}>
            <span class="from" title=${item.from}>${item.from}</span>
            <${Icon} name="arrow-right" size=${14} />
            <span class="to" title=${item.to}>${item.to}</span>
          </div>`)}
        </div>
        ${shown.total > shown.items.length && html`<p class="more-row"><${Button} onClick=${more}>Show more (${fmt.number(shown.total - shown.items.length)} left)<//></p>`}
        ${list !== 'moves' && html`<p class="subtle">${list === 'collisions' ? 'These songs stay where they are because another file or song already uses the new name. Nothing is overwritten.' : 'Windows cannot open paths this long, so these songs stay where they are. A shorter layout may help.'}</p>`}`}`}

      <${Batches} batches=${info.batches} busy=${busy} onUndo=${undo} />
      <p class="hint-card subtle"><${Icon} name="info" size=${14} /> A song that is playing, or open in another program, cannot be moved. It is skipped and reported, and a second run picks it up.</p>`}`;
}
