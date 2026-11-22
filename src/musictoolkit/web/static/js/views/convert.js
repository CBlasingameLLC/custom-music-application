// Convert songs: choose what to convert and into which format, preview every song, write the converted copies to a folder of your own, take a conversion back.
// The library itself is never changed: a conversion only makes new files, in a folder outside the library.
import { api, fmt, html, useAsync, useDebounced, useEffect, useRef, useState, useStore } from '../lib.js';
import { Icon } from '../icons.js';
import { Button, JobBanner, PageHeader, Spinner, TabPanel, Tabs } from '../components.js';
import { confirmDialog, pickFolder } from '../dialogs.js';
import { SourceEditor } from '../sources.js';
import { Examples } from './organize.js';
import { desktop, jobs, library, notifyError, onJobFinished, toast, trackJob } from '../state.js';

const KINDS = ['convert-preview', 'convert', 'convert-undo'];
const PAGE = 100;

const LISTS = [
  ['convert', 'Will be converted'],
  ['copy', 'Copied as they are'],
  ['skip', 'Left out'],
  ['exists', 'Already in the folder'],
  ['clash', 'Same name as another'],
  ['cannot', 'Cannot be converted'],
];
const LIST_NOTES = {
  copy: 'These songs are already in this format, at or below the quality you chose, so they are copied unchanged: converting them again would only lose quality.',
  skip: 'These songs are already in this format, and you chose to leave those out.',
  exists: 'A file with this name is already in the folder. Nothing is overwritten: these songs are left as they are.',
  clash: 'Two songs would get the same file name, so only the first is converted. A layout with the track number or the album in it keeps them apart.',
  cannot: 'These songs are left out. The reason is under each one.',
};
const RATES = [[0, 'No limit: keep each song’s own'], [48000, 'At most 48 kHz'], [44100, 'At most 44.1 kHz (CD)']];
const DEPTHS = [['16', '16-bit (CD quality, smaller files)'], ['keep', 'Keep the original (24-bit stays 24-bit)']];
const EXISTING = [['copy', 'Copy them as they are'], ['convert', 'Convert them again anyway'], ['skip', 'Leave them out']];
const STATES = { done: ['Done', 'good'], partial: ['Some failed', 'ok'], failed: ['Failed', 'low'], cancelled: ['Stopped', 'ok'], interrupted: ['Interrupted', 'ok'], running: ['Running', ''] };
const ITEM_STATES = { done: ['Made', 'good'], failed: ['Failed', 'low'], skipped: ['Not made', 'ok'], working: ['Not finished', 'ok'], waiting: ['Not started', 'ok'] };

const when = (iso) => {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : `${date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}, ${date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}`;
};

/** The form as the server's choices describe it: what was chosen last time where that is still offered, the defaults otherwise. */
function initialForm(info) {
  const last = info.last || {};
  const format = info.formats.find((f) => f.id === last.format) || info.formats.find((f) => f.id === 'mp3') || info.formats[0];
  const offered = (value, list, fallback) => (list.includes(value) ? value : fallback);
  return {
    sources: [],
    format: format.id,
    quality: format.lossy ? (format.qualities.some((q) => q.id === last.quality) ? last.quality : format.default) : null,
    destination: typeof last.destination === 'string' && last.destination ? last.destination : info.default_destination,
    scheme: typeof last.scheme === 'string' && last.scheme ? last.scheme : info.default_scheme,
    maxRate: offered(last.max_rate, info.max_rates, 0),
    depth: offered(last.depth, info.depths, '16'),
    existing: offered(last.existing, info.existing, 'copy'),
    cover: last.cover !== false,
  };
}

/** What the server is sent, for the preview and for the start: the same for both, or the start is refused. */
const bodyOf = (form, info) => ({
  format: form.format,
  quality: form.quality,
  destination: form.destination.trim(),
  scheme: !form.scheme.trim() || form.scheme.trim() === info.default_scheme ? null : form.scheme.trim(),
  max_rate: form.maxRate,
  depth: form.depth,
  existing: form.existing,
  cover: form.cover,
  sources: form.sources,
});

function summaryText(r) {
  const made = [];
  if (r.converted) made.push(`Converted ${fmt.plural(r.converted, 'song')}`);
  if (r.copied) made.push(`${made.length ? 'copied' : 'Copied'} ${fmt.plural(r.copied, 'song')} as ${r.copied === 1 ? 'it was' : 'they were'}`);
  let text = `${made.join(' and ') || 'Nothing was made'} in ${r.destination}.`;
  if (r.failed) text += ` ${fmt.plural(r.failed, 'song')} could not be converted: the history says why.`;
  if (r.previewed !== r.songs) text += ` The preview showed ${fmt.plural(r.previewed, 'song')}: files changed in between.`;
  return text;
}

// ------------------------------------------------------------ the history

function BatchDetails({ batch }) {
  const [only, setOnly] = useState(batch.failed ? 'not_made' : '');
  const [limit, setLimit] = useState(PAGE);
  const { data, error } = useAsync(() => api(`/tools/convert/batches/${batch.id}`, { params: { limit, only } }), [batch.id, only, limit, batch.status, batch.undone_at]);
  const options = batch.options || {};
  const settings = [
    options.max_rate ? `Sample rate at most ${options.max_rate / 1000} kHz` : 'Sample rate kept',
    batch.format === 'flac' || batch.format === 'wav' ? (options.depth === 'keep' ? 'Bit depth kept' : '16-bit') : null,
    `Songs already in the format: ${{ copy: 'copied as they were', convert: 'converted again', skip: 'left out' }[options.existing] || options.existing}`,
    options.cover === false ? 'No cover pictures' : 'Cover pictures carried over',
  ].filter(Boolean).join(' · ');
  return html`<div class="batch-detail" id=${`batch-${batch.id}`}>
    <dl class="info-grid">
      <dt>Folder</dt><dd class="path">${batch.destination}</dd>
      <dt>Layout</dt><dd><code>${batch.scheme}</code></dd>
      <dt>Settings</dt><dd>${settings}</dd>
      <dt>Result</dt><dd>${fmt.plural(batch.converted, 'song')} converted, ${fmt.number(batch.copied)} copied as they were, ${fmt.number(batch.failed)} failed, ${fmt.number(batch.left_out)} left out${batch.bytes ? ` · ${fmt.bytes(batch.bytes)}` : ''}</dd>
      <dt>Finished</dt><dd>${batch.finished_at ? when(batch.finished_at) : 'Not finished'}${batch.undone_at ? ` · taken back ${when(batch.undone_at)}` : ''}</dd>
    </dl>
    ${batch.message && html`<p class="note"><${Icon} name="info" size=${14} /> ${batch.message}</p>`}
    <div class="row-actions">
      <label class="field narrow-inline"><span>Show</span>
        <select aria-label="Which songs to show" value=${only} onChange=${(e) => { setLimit(PAGE); setOnly(e.target.value); }}>
          <option value="">All ${fmt.plural(batch.songs, 'song')}</option><option value="not_made">Only the songs that were not made</option>
        </select></label>
      ${desktop?.openPath && html`<${Button} small icon="folder" onClick=${() => desktop.openPath(batch.destination)}>Open folder<//>`}
    </div>
    ${error && html`<p class="subtle">${error.message}</p>`}
    ${!data && !error && html`<${Spinner} />`}
    ${data && html`<div class="move-table convert-items" role="table" aria-label=${`Songs in the conversion of ${batch.title}`}>
      <div class="move-head" role="row"><span role="columnheader">Song</span><span role="columnheader">Result</span><span role="columnheader">File or reason</span></div>
      ${data.items.map((item) => {
        const [label, tone] = ITEM_STATES[item.status] || [item.status, ''];
        return html`<div class="move-row" role="row" key=${item.id}>
          <span class="from" role="cell" title=${item.source}>${item.title}</span>
          <span role="cell"><span class="chip ${tone}">${label}</span></span>
          <span class="to" role="cell" title=${item.target}>${item.message || item.target}</span>
        </div>`;
      })}
      ${data.items.length === 0 && html`<div class="move-row" role="row"><span role="cell" class="subtle">${only ? 'Every song was made.' : 'This conversion has no songs.'}</span></div>`}
    </div>`}
    ${data && data.items_total > data.items.length && html`<p class="more-row"><${Button} onClick=${() => setLimit(limit + PAGE)}>Show more (${fmt.number(data.items_total - data.items.length)} left)<//></p>`}
  </div>`;
}

function BatchRow({ batch, busy, onUndo }) {
  const [open, setOpen] = useState(false);
  const made = batch.converted + batch.copied;
  const [label, tone] = batch.undone_at ? ['Taken back', ''] : STATES[batch.status] || [batch.status, ''];
  return html`<div class="batch-item">
    <div class="batch-row">
      <span class="batch-when">${when(batch.started_at)}</span>
      <span class="batch-what" title=${batch.destination}>${batch.title}</span>
      <span class="chip ${tone}">${label}</span>
      <span class="spacer"></span>
      <button type="button" class="chip-btn" aria-expanded=${open} aria-controls=${open ? `batch-${batch.id}` : undefined} onClick=${() => setOpen(!open)}>${open ? 'Hide details' : 'Details'}</button>
      ${!batch.undone_at && made > 0 && html`<${Button} small icon="undo" disabled=${busy} onClick=${() => onUndo(batch)}>Undo<//>`}
    </div>
    ${open && html`<${BatchDetails} batch=${batch} />`}
  </div>`;
}

// ------------------------------------------------------------ the screen

export function ConvertView() {
  const rev = useStore(library, (s) => s.rev);
  const { items: jobItems } = useStore(jobs);
  const [version, setVersion] = useState(0);
  const { data: info, error } = useAsync(() => api('/tools/convert/info'), [rev, version]);
  const { data: history } = useAsync(() => api('/tools/convert/batches'), [rev, version]);
  const [form, setForm] = useState(null);
  const [plan, setPlan] = useState(null); // { jobId, key, body, result, lists: { convert: { items, total }, ... } }
  const [list, setList] = useState('convert');
  const [pending, setPending] = useState(null); // the preview job being waited for
  const refresh = () => setVersion((v) => v + 1);
  const pendingRef = useRef(null);
  pendingRef.current = pending;
  const typed = useDebounced(form?.scheme || '', 250);
  const { data: example } = useAsync(() => (typed.trim() ? api('/tools/organize/example', { params: { scheme: typed } }) : null), [typed]);

  const busy = jobItems.some((j) => KINDS.includes(j.kind) && (j.status === 'running' || j.status === 'queued'));

  useEffect(() => {
    if (info && !form) setForm(initialForm(info));
  }, [info]);

  useEffect(() => onJobFinished((job) => {
    if (job.kind === 'convert-preview' && pendingRef.current && job.id === pendingRef.current.id) {
      const asked = pendingRef.current;
      setPending(null);
      pendingRef.current = null;
      if (job.status === 'done') loadPlan(job, asked.body);
    } else if (job.kind === 'convert') {
      refresh();
      setPlan(null);
      const r = job.result;
      if (job.status === 'done' && r) {
        const undoable = r.converted + r.copied > 0;
        toast(summaryText(r), r.failed ? 'info' : 'success', 15000, undoable ? { label: 'Undo', onClick: () => undo({ id: r.conversion_id, destination: r.destination, converted: r.converted, copied: r.copied }) } : null);
      } else if (job.status === 'cancelled') {
        toast('Stopped. The songs already converted stay in the folder, and the history lets you take them back.', 'info', 10000);
      }
    } else if (job.kind === 'convert-undo') {
      refresh();
      const r = job.result;
      if (job.status === 'done' && r) {
        const notes = [r.changed && `${fmt.plural(r.changed, 'file')} had been changed since and ${r.changed === 1 ? 'was' : 'were'} left alone`, r.gone && `${fmt.plural(r.gone, 'file')} ${r.gone === 1 ? 'was' : 'were'} already gone`, r.failed && `${fmt.plural(r.failed, 'file')} could not be deleted`].filter(Boolean);
        toast(`Took back ${fmt.plural(r.removed, 'converted song')}.${notes.length ? ` ${notes.join('; ')}.` : ''}`, notes.length ? 'info' : 'success', 10000);
      }
    }
  }), []);

  async function loadPlan(job, body) {
    try {
      const result = job.result;
      const first = LISTS.find(([kind]) => result.counts[kind] > 0)?.[0] || 'convert';
      const page = await api(`/tools/convert/preview/${job.id}`, { params: { action: first, limit: PAGE } });
      setPlan({ jobId: job.id, key: JSON.stringify(body), body, result, lists: { [first]: page } });
      setList(first);
    } catch (err) {
      notifyError(err);
    }
  }

  async function showList(kind) {
    setList(kind);
    if (!plan || plan.lists[kind]) return;
    try {
      const page = await api(`/tools/convert/preview/${plan.jobId}`, { params: { action: kind, limit: PAGE } });
      setPlan((p) => p && { ...p, lists: { ...p.lists, [kind]: page } });
    } catch (err) {
      notifyError(err);
    }
  }

  async function more() {
    const have = plan.lists[list];
    try {
      const page = await api(`/tools/convert/preview/${plan.jobId}`, { params: { action: list, offset: have.items.length, limit: PAGE } });
      setPlan((p) => p && { ...p, lists: { ...p.lists, [list]: { ...page, items: [...have.items, ...page.items] } } });
    } catch (err) {
      notifyError(err);
    }
  }

  async function runPreview() {
    setPlan(null);
    const body = bodyOf(form, info);
    try {
      const { job } = await api('/tools/convert/preview', { method: 'POST', body });
      pendingRef.current = { id: job.id, body }; // before the job can finish, not after the next render
      setPending(pendingRef.current);
      trackJob(job);
    } catch (err) {
      notifyError(err);
    }
  }

  async function start() {
    try {
      const { job } = await api('/tools/convert/start', { method: 'POST', body: plan.body });
      trackJob(job);
    } catch (err) {
      notifyError(err);
    }
  }

  async function undo(batch) {
    const made = batch.converted + batch.copied;
    const ok = await confirmDialog({
      title: `Take back ${fmt.plural(made, 'converted song')}?`,
      message: `The ${made === 1 ? 'file' : 'files'} this conversion made in ${batch.destination} will be deleted, and the folders that are left empty. A file you have changed since is left alone. Your library is not touched.`,
      confirm: 'Delete the converted files',
      danger: true,
    });
    if (!ok) return;
    try {
      const { job } = await api('/tools/convert/undo', { method: 'POST', body: { conversion_id: batch.id } });
      trackJob(job);
    } catch (err) {
      notifyError(err);
    }
  }

  async function browse() {
    const chosen = await pickFolder('Choose the folder for the converted songs');
    if (chosen) setForm((f) => ({ ...f, destination: chosen }));
  }

  if (error) return html`<${PageHeader} title="Convert songs" /><p class="subtle">${error.message}</p>`;
  if (!info || !form) return html`<${PageHeader} title="Convert songs" /><${Spinner} />`;

  const set = (patch) => setForm((f) => ({ ...f, ...patch }));
  const format = info.formats.find((f) => f.id === form.format);
  const chooseFormat = (id) => {
    const next = info.formats.find((f) => f.id === id);
    set({ format: id, quality: next.lossy ? (next.qualities.some((q) => q.id === form.quality) ? form.quality : next.default) : null });
  };
  const insert = (name) => set({ scheme: form.scheme + `{${name}}` });
  const preset = info.presets.find((p) => p.scheme === form.scheme)?.scheme || '';

  const asked = JSON.stringify(bodyOf(form, info));
  const current = plan && plan.key === asked;
  const stale = plan && !current;
  const result = plan?.result;
  const counts = result?.counts;
  const writing = counts ? counts.convert + counts.copy : 0;
  const shown = plan?.lists[list];
  const ready = form.sources.length > 0 && form.destination.trim() && form.scheme.trim() && example?.ok !== false;

  return html`
    <${PageHeader} title="Convert songs" subtitle="Make copies of your songs in another format, in a folder of your own. Tags and covers come along, your library is not touched, and a whole batch can be undone." />
    <div class="organize-form convert-form">
      <div class="field wide">
        <span aria-hidden="true">What to convert</span>
        <${SourceEditor} sources=${form.sources} disabled=${busy} label="What to convert" empty="Nothing chosen yet. Add the music to convert." onChange=${(sources) => set({ sources })} />
      </div>
      <label class="field">
        <span>Format</span>
        <select aria-label="Format" value=${form.format} disabled=${busy} onChange=${(e) => chooseFormat(e.target.value)}>
          ${info.formats.map((f) => html`<option value=${f.id} key=${f.id}>${f.label}${f.lossy ? '' : ' (lossless)'}</option>`)}
        </select>
      </label>
      ${format.lossy
        ? html`<label class="field">
            <span>Quality</span>
            <select aria-label="Quality" value=${form.quality} disabled=${busy} onChange=${(e) => set({ quality: e.target.value })}>
              ${format.qualities.map((q) => html`<option value=${q.id} key=${q.id}>${q.label}</option>`)}
            </select>
          </label>`
        : html`<label class="field">
            <span>Bit depth</span>
            <select aria-label="Bit depth" value=${form.depth} disabled=${busy} onChange=${(e) => set({ depth: e.target.value })}>
              ${DEPTHS.filter(([id]) => info.depths.includes(id)).map(([id, label]) => html`<option value=${id} key=${id}>${label}</option>`)}
            </select>
          </label>`}
      <p class="field wide subtle format-blurb">${format.blurb}</p>
      <label class="field">
        <span>Sample rate</span>
        <select aria-label="Sample rate" value=${form.maxRate} disabled=${busy} onChange=${(e) => set({ maxRate: Number(e.target.value) })}>
          ${RATES.filter(([rate]) => info.max_rates.includes(rate)).map(([rate, label]) => html`<option value=${rate} key=${rate}>${label}</option>`)}
        </select>
      </label>
      <label class="field">
        <span>Songs already in this format</span>
        <select aria-label="Songs already in this format" value=${form.existing} disabled=${busy} onChange=${(e) => set({ existing: e.target.value })}>
          ${EXISTING.filter(([id]) => info.existing.includes(id)).map(([id, label]) => html`<option value=${id} key=${id}>${label}</option>`)}
        </select>
      </label>
      <div class="field wide">
        <span id="convert-folder-label">Folder for the converted songs</span>
        <div class="inline-form">
          <input id="convert-folder" class="mono" type="text" spellcheck="false" aria-labelledby="convert-folder-label" value=${form.destination} disabled=${busy} onInput=${(e) => set({ destination: e.target.value })} />
          <${Button} icon="folder" disabled=${busy} onClick=${browse}>Browse…<//>
        </div>
        <span class="subtle">It must be outside your music folders${info.roots.length ? `: ${info.roots.join(', ')}` : ''}. Converted songs would show up in the library twice otherwise.</span>
      </div>
      <label class="field">
        <span>Layout</span>
        <select aria-label="Layout preset" value=${preset} disabled=${busy} onChange=${(e) => e.target.value && set({ scheme: e.target.value })}>
          <option value="">Custom</option>
          ${info.presets.map((p) => html`<option value=${p.scheme} key=${p.scheme}>${p.name}</option>`)}
        </select>
      </label>
      <label class="check cover-check">
        <input type="checkbox" checked=${form.cover} disabled=${busy} onChange=${(e) => set({ cover: e.target.checked })} /> Carry each song’s cover picture into its copy
      </label>
      <label class="field wide">
        <span>Folder and file name, built from these fields</span>
        <input class="mono" type="text" spellcheck="false" value=${form.scheme} disabled=${busy} onInput=${(e) => set({ scheme: e.target.value })} />
      </label>
      <div class="field-chips" aria-label="Insert a field">
        ${info.fields.map((f) => html`<button class="chip-btn" type="button" key=${f.name} title=${f.help} disabled=${busy} onClick=${() => insert(f.name)}>{${f.name}}</button>`)}
      </div>
      <${Examples} example=${example} />
      <div class="row-actions">
        <${Button} kind="primary" icon="search" disabled=${busy || !ready} onClick=${runPreview}>Preview<//>
        ${form.sources.length === 0 && html`<span class="note"><${Icon} name="info" size=${14} /> Choose what to convert first.</span>`}
      </div>
    </div>
    <${JobBanner} kinds=${KINDS} />

    ${stale && html`<div class="callout"><p><${Icon} name="info" size=${16} /><span>You changed something after previewing. Preview again to see what would happen now.</span></p></div>`}

    ${current && result && html`
      ${result.problems.length > 0 && html`<div class="callout warn" role="alert">
        ${result.problems.map((problem) => html`<p key=${problem}><${Icon} name="alert" size=${16} /><span>${problem}</span></p>`)}
      </div>`}
      <div class="stat-row">
        <div class="stat"><strong>${fmt.number(counts.convert)}</strong><span>${counts.convert === 1 ? 'song' : 'songs'} will be converted</span></div>
        ${counts.copy > 0 && html`<div class="stat"><strong>${fmt.number(counts.copy)}</strong><span>copied as ${counts.copy === 1 ? 'it is' : 'they are'}</span></div>`}
        ${counts.exists > 0 && html`<div class="stat"><strong>${fmt.number(counts.exists)}</strong><span>already in the folder</span></div>`}
        ${counts.cannot + counts.clash + counts.skip > 0 && html`<div class="stat"><strong>${fmt.number(counts.cannot + counts.clash + counts.skip)}</strong><span>left out</span></div>`}
        <div class="stat"><strong>About ${fmt.bytes(result.bytes)}</strong><span>to write${result.free_bytes != null ? ` · ${fmt.bytes(result.free_bytes)} free` : ''}</span></div>
        <div class="stat"><strong>${fmt.duration(result.seconds)}</strong><span>of music</span></div>
      </div>
      ${result.ok && writing > 0 && html`<div class="organize-apply">
        <${Button} kind="primary" icon="refresh" disabled=${busy} onClick=${start}>Convert ${fmt.plural(writing, 'song')}<//>
        <span class="subtle">Into <code>${result.destination}</code>. Nothing there is overwritten, and your library is not changed.</span>
      </div>`}
      <${Tabs} id="convert" label="Songs by what happens to them" value=${list} onChange=${showList}
        tabs=${LISTS.filter(([kind]) => kind === 'convert' || counts[kind] > 0).map(([kind, label]) => ({ id: kind, label, count: fmt.number(counts[kind]) }))} />
      <${TabPanel} id="convert" value=${list}>
        ${shown && html`<div class="move-table convert-table" role="table" aria-label=${LISTS.find(([k]) => k === list)[1]}>
          <div class="move-head" role="row"><span role="columnheader">Song</span><span></span><span role="columnheader">${list === 'convert' || list === 'copy' ? 'File that will be made' : 'Would have been'}</span></div>
          ${shown.items.map((item, i) => html`<div class="move-row" role="row" key=${`${item.id}-${i}`}>
            <span class="from" role="cell" title=${item.from}>${item.title}${item.artist ? ` · ${item.artist}` : ''}
              ${item.notes.map((note) => html`<small key=${note}>${note}</small>`)}</span>
            <${Icon} name="arrow-right" size=${14} />
            <span class="to" role="cell" title=${item.to}>${item.to}</span>
          </div>`)}
          ${shown.items.length === 0 && html`<div class="move-row" role="row"><span role="cell" class="subtle">Nothing here.</span></div>`}
        </div>
        ${shown.total > shown.items.length && html`<p class="more-row"><${Button} onClick=${more}>Show more (${fmt.number(shown.total - shown.items.length)} left)<//></p>`}
        ${LIST_NOTES[list] && html`<p class="subtle">${LIST_NOTES[list]}</p>`}`}
      <//>`}

    ${history?.batches?.length > 0 && html`<h2 class="section-title">Recent conversions</h2>
      <div class="batch-list">
        ${history.batches.map((batch) => html`<${BatchRow} key=${batch.id} batch=${batch} busy=${busy} onUndo=${undo} />`)}
      </div>`}
    <p class="hint-card subtle"><${Icon} name="info" size=${14} /> <span>Converting makes new files and never changes or deletes your songs. A lossy song (MP3, Ogg, Opus, M4A…) does not get its quality back by being converted to FLAC or WAV.${' '}
      ${info.windows_decoding.available ? 'M4A, AAC and WMA songs are read by Windows, so they convert like any other.' : 'M4A, AAC and WMA songs can only be read on Windows, so here they are listed as songs that cannot be converted.'}</span></p>`;
}
