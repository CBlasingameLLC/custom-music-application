// Library tools: the hub, and the screen that fixes missing tags with MusicBrainz.
import { api, fmt, html, useAsync, useEffect, useState, useStore } from '../lib.js';
import { Icon } from '../icons.js';
import { Button, Empty, JobBanner, PageHeader, Spinner } from '../components.js';
import { href, jobs, library, loadSettings, notifyError, onJobFinished, toast, trackJob } from '../state.js';

// ------------------------------------------------------------ hub

const TOOLS = [
  {
    path: '/tools/enrich',
    icon: 'tag',
    title: 'Fix missing tags',
    blurb: 'Songs with no title, artist or album are looked up on MusicBrainz. You approve each match before anything is written.',
    status: (d) => (d.enrich ? (d.enrich.pending ? `${fmt.plural(d.enrich.pending, 'match', 'matches')} to review` : d.enrich.to_look_up ? `${fmt.plural(d.enrich.to_look_up, 'song')} to look up` : 'Nothing needs fixing') : ''),
  },
  {
    path: '/tools/organize',
    icon: 'folder',
    title: 'Organize files',
    blurb: 'Rename and move songs into folders like Artist / Album / 01 - Title. You see every move first, lyrics and covers follow, and a whole batch can be undone.',
    status: (d) => (d.organize?.batches?.length ? `Last change ${fmt.ago(Date.parse(d.organize.batches[0].moved_at) / 1000)}` : ''),
  },
];

export function ToolsView() {
  const rev = useStore(library, (s) => s.rev);
  const { data: enrich } = useAsync(() => api('/tools/enrich/summary'), [rev]);
  const { data: organize } = useAsync(() => api('/tools/organize/batches'), [rev]);
  const data = { enrich, organize };
  return html`
    <${PageHeader} title="Library tools" subtitle="Keep your collection tidy. Every tool shows a preview first and can be undone." />
    <div class="tool-grid">
      ${TOOLS.map((tool) => html`<a class="tool-card" href=${href(tool.path)} key=${tool.path}>
        <span class="tool-icon"><${Icon} name=${tool.icon} size=${22} /></span>
        <h3>${tool.title}</h3>
        <p>${tool.blurb}</p>
        <span class="tool-status">${tool.status(data) || ' '}</span>
      </a>`)}
    </div>`;
}

// ------------------------------------------------------------ fix missing tags

const FIELDS = [['title', 'Title'], ['artist', 'Artist'], ['album', 'Album']];
const PAGE = 50;

function confidenceClass(value) {
  return value >= 0.9 ? 'good' : value >= 0.75 ? 'ok' : 'low';
}

function ContactForm({ onSaved }) {
  const [email, setEmail] = useState('');
  async function save(event) {
    event.preventDefault();
    try {
      await api('/settings', { method: 'PUT', body: { musicbrainz: { contact: email.trim() } } });
      await loadSettings();
      onSaved();
    } catch (error) {
      notifyError(error);
    }
  }
  return html`<form class="callout warn" onSubmit=${save}>
    <p><${Icon} name="info" size=${16} /> MusicBrainz asks every app to send a contact email with its requests, so they can reach you if it misbehaves. It is not used for anything else.</p>
    <div class="inline-form">
      <input type="email" required placeholder="you@example.com" aria-label="Contact email" value=${email} onInput=${(e) => setEmail(e.target.value)} />
      <${Button} kind="primary" type="submit" disabled=${!email.includes('@')}>Save<//>
    </div>
  </form>`;
}

export function EnrichView() {
  const rev = useStore(library, (s) => s.rev);
  const { items: jobItems } = useStore(jobs);
  const [version, setVersion] = useState(0);
  const [overwrite, setOverwrite] = useState(false);
  const [minConfidence, setMinConfidence] = useState(0);
  const [limit, setLimit] = useState(PAGE);
  const [selected, setSelected] = useState(() => new Set());
  const refresh = () => setVersion((v) => v + 1);

  const { data: summary } = useAsync(() => api('/tools/enrich/summary'), [rev, version]);
  const { data: list } = useAsync(
    () => api('/tools/enrich/proposals', { params: { limit, min_confidence: minConfidence, overwrite } }),
    [rev, version, limit, minConfidence, overwrite],
  );
  const { data: strong } = useAsync(() => api('/tools/enrich/proposals', { params: { limit: 1, min_confidence: 0.9 } }), [rev, version]);
  const lookingUp = jobItems.some((j) => j.kind === 'enrich' && (j.status === 'running' || j.status === 'queued'));

  useEffect(() => onJobFinished((job) => job.kind === 'enrich' && refresh()), []);
  useEffect(() => setSelected(new Set()), [minConfidence, rev]);

  async function lookUp() {
    try {
      const { job } = await api('/tools/enrich/start', { method: 'POST', body: {} });
      trackJob(job);
    } catch (error) {
      notifyError(error);
    }
  }
  async function apply(body, label) {
    try {
      const { job } = await api('/tools/enrich/apply', { method: 'POST', body: { ...body, overwrite } });
      trackJob(job);
      setSelected(new Set());
      toast(`Writing tags to ${label}…`);
    } catch (error) {
      notifyError(error);
    }
  }
  async function dismiss() {
    try {
      const { dismissed } = await api('/tools/enrich/dismiss', { method: 'POST', body: { track_ids: [...selected] } });
      toast(`Dismissed ${fmt.plural(dismissed, 'match', 'matches')}. Those songs keep their tags.`);
      setSelected(new Set());
      refresh();
    } catch (error) {
      notifyError(error);
    }
  }
  async function retry() {
    try {
      const { reset } = await api('/tools/enrich/retry', { method: 'POST' });
      toast(`${fmt.plural(reset, 'song')} will be looked up again.`);
      refresh();
    } catch (error) {
      notifyError(error);
    }
  }

  const toggle = (id) => setSelected((s) => { const next = new Set(s); next.has(id) ? next.delete(id) : next.add(id); return next; });
  const items = list?.items || [];
  const allShown = items.length > 0 && items.every((i) => selected.has(i.track_id));

  return html`
    <${PageHeader} title="Fix missing tags" subtitle="Songs missing a title, artist or album are looked up on MusicBrainz. Nothing is written until you approve it.">
      <${Button} kind="primary" icon="search" disabled=${lookingUp || !summary?.contact_set || !summary?.to_look_up} onClick=${lookUp}>
        ${summary?.to_look_up ? `Look up ${fmt.plural(summary.to_look_up, 'song')}` : 'Look up songs'}<//>
    <//>
    ${!summary ? html`<${Spinner} />` : html`
      <div class="stat-row">
        <div class="stat"><strong>${fmt.number(summary.to_look_up)}</strong><span>to look up</span></div>
        <div class="stat"><strong>${fmt.number(summary.pending)}</strong><span>matches to review</span></div>
        <div class="stat"><strong>${fmt.number(summary.applied)}</strong><span>fixed</span></div>
        <div class="stat"><strong>${fmt.number(summary.no_match)}</strong><span>no match found</span></div>
      </div>
      ${!summary.contact_set && html`<${ContactForm} onSaved=${refresh} />`}`}
    <${JobBanner} kinds=${['enrich']} />

    ${summary && list && summary.pending === 0 && !lookingUp && html`<${Empty} icon="tag" title=${summary.to_look_up ? 'Ready to look up' : 'All caught up'}>
      <p>${summary.to_look_up
        ? `${fmt.plural(summary.to_look_up, 'song')} ${summary.to_look_up === 1 ? 'is' : 'are'} missing a title, artist or album. Press “Look up” to find matches, about a second per song.`
        : 'Every song in your library has a title, artist and album.'}</p>
      ${(summary.no_match > 0 || summary.dismissed > 0) && html`<p><button class="link" onClick=${retry}>Try again for the ${fmt.number(summary.no_match + summary.dismissed)} songs with no match or a dismissed one</button></p>`}
    <//>`}

    ${list && summary?.pending > 0 && html`
      <div class="enrich-bar">
        <label class="check"><input type="checkbox" checked=${allShown} onChange=${() => setSelected(allShown ? new Set() : new Set(items.map((i) => i.track_id)))} /> ${selected.size ? `${fmt.plural(selected.size, 'match', 'matches')} selected` : `Select shown`}</label>
        <label class="sort-select"><span>Show</span><select value=${minConfidence} onChange=${(e) => setMinConfidence(Number(e.target.value))}>
          <option value="0">All matches</option><option value="0.75">75% and up</option><option value="0.9">90% and up</option></select></label>
        <label class="check" title="Off: only empty tags are filled in. On: MusicBrainz's spelling replaces what is there."><input type="checkbox" checked=${overwrite} onChange=${(e) => setOverwrite(e.target.checked)} /> Replace existing tags too</label>
        <span class="spacer"></span>
        <${Button} small icon="x" disabled=${!selected.size} onClick=${dismiss}>Dismiss<//>
        <${Button} small icon="check" disabled=${!selected.size} onClick=${() => apply({ track_ids: [...selected] }, fmt.plural(selected.size, 'song'))}>Apply selected<//>
        <${Button} small kind="primary" icon="check" disabled=${!strong?.total} onClick=${() => apply({ min_confidence: 0.9 }, fmt.plural(strong.total, 'song'))}>Apply all 90%+ (${fmt.number(strong?.total || 0)})<//>
      </div>
      <div class="enrich-table" role="table" aria-label="Matches to review">
        <div class="enrich-head" role="row"><span></span><span>File</span>${FIELDS.map(([, label]) => html`<span>${label}</span>`)}<span>Match</span></div>
        ${items.map((item) => html`<label class="enrich-row ${selected.has(item.track_id) ? 'selected' : ''}" role="row" key=${item.track_id}>
          <input type="checkbox" checked=${selected.has(item.track_id)} onChange=${() => toggle(item.track_id)} aria-label=${`Select ${item.file}`} />
          <span class="file" title=${item.path}>${item.file}</span>
          ${FIELDS.map(([key]) => html`<span class="cell" key=${key}>
            ${item.will_write[key]
              ? html`<strong class="new">${item.will_write[key]}</strong>${item.current[key] ? html`<s class="subtle">${item.current[key]}</s>` : ''}`
              : html`<span class="subtle">${item.current[key] || '—'}</span>`}
          </span>`)}
          <span class="chip ${confidenceClass(item.confidence)}">${Math.round(item.confidence * 100)}%</span>
        </label>`)}
      </div>
      ${list.total > items.length && html`<p class="more-row"><${Button} onClick=${() => setLimit(limit + PAGE)}>Show more (${fmt.number(list.total - items.length)} left)<//></p>`}
      ${list.total === 0 && html`<p class="subtle">No matches at this confidence.</p>`}`}
    <p class="hint-card subtle"><${Icon} name="info" size=${14} /> Applying writes into the music files and can be undone from the “Updated tags” message right afterwards. Dismissed songs are not looked up again unless you ask.</p>`;
}
