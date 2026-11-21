// Burning: songs from the library onto a music CD or a data disc, or into an ISO file; erasing a rewritable disc; the history of burns.
// Nothing is written until the list has been checked (the server insists), and a burn cannot be taken back, so it is confirmed first.
import { api, fmt, html, useAsync, useDebounced, useEffect, useLayoutEffect, useRef, useState, useStore } from '../lib.js';
import { Icon } from '../icons.js';
import { Button, Empty, JobBanner, Spinner } from '../components.js';
import { confirmDialog } from '../dialogs.js';
import { burnList, clearBurnList, desktop, jobs, moveInBurnList, notifyError, removeFromBurnList, trackJob } from '../state.js';
import { useDrives, when } from './discparts.js';

const KINDS = [
  { id: 'audio', label: 'Music CD', hint: 'Plays in any CD player or car stereo. The songs are converted to CD audio (Windows decodes M4A, AAC and WMA songs first). Needs a blank CD-R or CD-RW.' },
  { id: 'data', label: 'Data disc', hint: 'Keeps the song files as they are, for a computer, an MP3 player or a car stereo that reads files. On a CD, a DVD or a Blu-ray disc. Every file is read back afterwards and checked.' },
  { id: 'iso', label: 'Disc image (ISO file)', hint: 'The same as a data disc, as one file to burn later or to keep. No disc is needed.' },
];
const LAYOUTS = [
  ['library', 'The same folders as in my library'],
  ['flat', 'One folder, numbered in this order (for car stereos and MP3 players)'],
];
const VERDICT = { ok: 'Will be burned', converted: 'Will be converted', cannot: 'Cannot be burned' };
const CHIP = { ok: 'new', converted: 'missing', cannot: 'damaged', done: 'new', failed: 'damaged', skipped: '', waiting: '' };
const BURN_STATUS = { running: 'Burning', done: 'Done', cancelled: 'Stopped', failed: 'Failed', interrupted: 'Interrupted' };
const ITEM_STATUS = { waiting: 'Waiting', done: 'Written', skipped: 'Left out', failed: 'Not written' };
const ENDPOINTS = { audio: 'burn', data: 'data', iso: 'iso' };
const GAPS = { none: 'no gaps', '2s': '2-second gaps' };

/** How much of a disc, in the units that kind of disc is measured in: minutes for music, bytes for files. */
const room = (sectors, kind) => (kind === 'audio' ? fmt.duration((sectors || 0) / 75) : fmt.bytes((sectors || 0) * 2048));

function discWords(m) {
  if (!m) return 'Looking at the disc…';
  if (!m.writer) return 'This drive cannot write discs.';
  if (m.state === 'none') return 'No disc in the drive';
  if (m.state === 'blank') return `Blank ${m.type}, ${room(m.free_sectors, m.type?.startsWith('CD') ? 'audio' : 'data')} free`;
  if (m.state === 'unusable') return 'A disc this drive cannot write to';
  return `${m.type || 'A disc'} with something on it${m.rewritable ? '. It can be erased' : ''}`;
}

// ------------------------------------------------------------ the list of songs

function BurnSongs({ items, rows, kind, disabled }) {
  if (!items.length) {
    return html`<${Empty} icon="cd" title="No songs on the list yet" level=${3}>
      <p>Choose “Add to burn list” in the menu of a song, on an album or on a playlist. The songs are burned in the order they are listed here.</p>
    <//>`;
  }
  const total = items.reduce((sum, i) => sum + (i.seconds || 0), 0);
  /** Move a song, and keep the focus on it: the same button, at the song's new place. */
  function moved(index, by) {
    moveInBurnList(index, by);
    const after = index + by;
    setTimeout(() => {
      const target = document.getElementById(`burn-${by < 0 ? 'up' : 'down'}-${after}`);
      (target && !target.disabled ? target : document.getElementById(`burn-${by < 0 ? 'down' : 'up'}-${after}`))?.focus();
    }, 0);
  }
  return html`<p class="subtle burn-total" role="status">${fmt.plural(items.length, 'song')} · ${fmt.duration(total)}</p>
    <ol class="rip-tracks burn-songs" aria-label="Songs to burn, in order">
      ${items.map((item, i) => {
        const row = rows?.[i + 1];
        const verdict = row ? (kind === 'audio' ? row.verdict : row.dest ? 'ok' : 'cannot') : null;
        const notes = row ? (kind === 'audio' ? row.notes : row.problem ? [row.problem] : []) : [];
        return html`<li key=${i} class=${verdict || ''}>
          <span class="num">${i + 1}</span>
          <span class="rip-song"><strong>${item.title || 'Untitled'}</strong>
            <span class="subtle">${[item.artist, item.seconds ? fmt.time(item.seconds) : ''].filter(Boolean).join(' · ')}</span>
            ${notes.map((n) => html`<span class="subtle burn-note">${n}</span>`)}
            ${kind !== 'audio' && row?.dest ? html`<span class="subtle mono">${row.dest}</span>` : ''}</span>
          <span class="burn-song-end">
            ${verdict && html`<span class="reason ${CHIP[verdict] || ''}">${kind === 'audio' ? VERDICT[verdict] : verdict === 'ok' ? 'Will be copied' : 'Left out'}</span>`}
            <button type="button" class="icon-btn" id=${`burn-up-${i}`} aria-label=${`Move ${item.title || 'song'} up`} title="Move up" disabled=${disabled || i === 0}
              onClick=${() => moved(i, -1)}><${Icon} name="chevron-up" size=${16} /></button>
            <button type="button" class="icon-btn" id=${`burn-down-${i}`} aria-label=${`Move ${item.title || 'song'} down`} title="Move down" disabled=${disabled || i === items.length - 1}
              onClick=${() => moved(i, 1)}><${Icon} name="chevron-down" size=${16} /></button>
            <button type="button" class="icon-btn" aria-label=${`Take ${item.title || 'song'} off the list`} title="Take off the list" disabled=${disabled}
              onClick=${() => removeFromBurnList(i)}><${Icon} name="x" size=${16} /></button>
          </span>
        </li>`;
      })}
    </ol>`;
}

// ------------------------------------------------------------ what a burn came to

function describeBurn(burn) {
  if (burn.status === 'running') return null;
  const wrote = burn.tracks.filter((t) => t.status === 'done').length;
  const what = burn.kind === 'audio' ? fmt.plural(wrote, 'song') : fmt.plural(wrote, 'file');
  if (burn.status === 'done') {
    if (burn.kind === 'iso') return `The disc image was saved as ${burn.output}.`;
    if (burn.kind === 'erase') return burn.message || 'The disc was erased.';
    if (burn.kind === 'audio') return `The music CD is ready: ${what}. Music CDs are not read back, so listen to it once to be sure.`;
    return burn.verified ? `The disc is ready: ${what}, and every file was read back from the disc and matches.` : `The disc is ready: ${what}.`;
  }
  return burn.message || 'The burn did not finish.';
}

function BurnProgress({ burnId, onNew }) {
  const [burn, setBurn] = useState(null);
  const heading = useRef();
  const { items } = useStore(jobs);
  const job = items.find((j) => ['burn', 'iso', 'erase'].includes(j.kind) && (j.status === 'running' || j.status === 'queued'));
  const running = burn ? burn.status === 'running' : true;
  useLayoutEffect(() => heading.current?.focus({ preventScroll: true }), []);
  useEffect(() => {
    let alive = true;
    let timer = null;
    const fetchIt = async () => {
      try {
        const detail = await api(`/discs/burns/${burnId}`);
        if (alive) setBurn(detail);
        if (detail.status !== 'running') clearInterval(timer);
      } catch (error) {
        if (alive) notifyError(error);
      }
    };
    fetchIt();
    timer = setInterval(fetchIt, 800);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [burnId, job?.status]);
  const words = burn && describeBurn(burn);
  const good = burn && burn.status === 'done';
  const eraseOnly = burn?.kind === 'erase';
  return html`<section aria-labelledby="burn-title">
    <h2 id="burn-title" class="section-title" tabIndex="-1" ref=${heading}>${burn ? burn.title : 'Burning'}</h2>
    <${JobBanner} kinds=${['burn', 'iso', 'erase']} cancelable=${!eraseOnly} />
    ${burn && !running && html`<div class="callout ${good ? '' : 'warn'}" role="status"><p><${Icon} name=${good ? 'check' : 'alert'} size=${16} /><span>${words}</span></p></div>`}
    ${burn && !eraseOnly && html`<ol class="rip-tracks" aria-label="Songs and files of this burn">
      ${burn.tracks.map((t) => html`<li key=${t.position} class=${t.status}>
        <span class="num">${t.position}</span>
        <span class="rip-song"><strong>${t.title}</strong>${t.message && t.status !== 'waiting' ? html`<span class="subtle">${t.message}</span>` : ''}</span>
        <span class="reason ${CHIP[t.status] || ''}">${running && t.status === 'waiting' ? html`<span class="spinner small"></span>` : ''} ${ITEM_STATUS[t.status] || t.status}</span>
      </li>`)}
    </ol>`}
    ${burn && !running && html`<div class="row-actions">
      ${burn.kind === 'iso' && good && desktop?.showItemInFolder && html`<${Button} icon="folder" onClick=${() => desktop.showItemInFolder(burn.output)}>Show the file<//>`}
      <${Button} kind="primary" icon="cd" onClick=${onNew}>${eraseOnly ? 'Done' : 'Burn something else'}<//>
    </div>`}
  </section>`;
}

// ------------------------------------------------------------ erasing

function EraseCard({ drive, media, busy, onStarted }) {
  const [full, setFull] = useState(false);
  const can = drive && media?.writer && media.rewritable && media.state !== 'none' && media.state !== 'blank';
  async function erase() {
    const ok = await confirmDialog({
      title: 'Erase this disc?',
      message: `Everything on the ${media.type} in drive ${drive.id} is deleted, and the disc is blank again. It cannot be undone. ${full ? 'A full erase can take many minutes. ' : ''}It cannot be stopped once it has begun.`,
      confirm: 'Erase the disc',
      danger: true,
    });
    if (!ok) return;
    try {
      const { job, burn_id: burnId } = await api('/discs/erase', { method: 'POST', body: { drive: drive.id, full, confirm: true } });
      trackJob(job);
      onStarted(burnId);
    } catch (error) {
      notifyError(error);
    }
  }
  return html`<h2 class="section-title">Erase a rewritable disc</h2>
    <p class="subtle">A CD-RW or DVD-RW can be erased and used again. ${media?.state === 'blank' ? 'The disc in the drive is blank already.' : can ? '' : 'Put a rewritable disc that has something on it in the drive to enable this.'}</p>
    <div class="option-list">
      <label class="check-row"><input type="checkbox" checked=${full} disabled=${!can} onChange=${(e) => setFull(e.target.checked)} /><span>Erase everything on the disc (a full erase: slower, but nothing can be recovered)</span></label>
    </div>
    <div class="row-actions"><${Button} icon="trash" kind="danger" disabled=${!can || busy} onClick=${erase}>Erase this disc…<//></div>`;
}

// ------------------------------------------------------------ the tab

export function BurnTab({ active }) {
  const { items } = useStore(burnList);
  const { items: jobItems } = useStore(jobs);
  const working = jobItems.some((j) => ['burn', 'erase', 'iso'].includes(j.kind) && (j.status === 'running' || j.status === 'queued'));
  const { data: info } = useAsync(() => api('/discs/options'), []);
  const [kind, setKind] = useState('audio');
  const [drives, look] = useDrives(active && !working);
  const [driveId, setDriveId] = useState('');
  const [media, setMedia] = useState(null); // null | { loading } | { error } | { data }
  const [reads, setReads] = useState(0);
  const [choices, setChoices] = useState(null); // speed, eject, verify, layout, label, folder
  const [plan, setPlan] = useState(null); // { key, plan } or { key, error }
  const [previewing, setPreviewing] = useState(false);
  const [again, setAgain] = useState(0);
  const [current, setCurrent] = useState(null); // the burn being watched: its id
  const [resumed, setResumed] = useState(false);
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);
  const visible = useRef(active); // (read by the window's focus handler, which is made once)
  visible.current = active;

  const writers = (drives.data?.drives || []).filter((d) => d.writer);
  const drive = writers.find((d) => d.id === driveId) || writers[0];
  const needsDrive = kind !== 'iso';

  // The defaults from Settings, once.
  useEffect(() => {
    if (!info || choices) return;
    setChoices({ speed: null, eject: info.burn.eject, verify: info.burn.verify, gapless: info.burn.gapless, layout: 'library', label: '', folder: info.iso_folder || '' });
  }, [info]);

  // A burn that is still going (the page was left and came back, or reloaded) is picked up again.
  useEffect(() => {
    api('/discs/burns').then(
      ({ burns }) => {
        if (burns[0] && burns[0].status === 'running') setCurrent((now) => now ?? burns[0].id);
        setResumed(true);
      },
      () => setResumed(true),
    );
  }, []);

  // What is in the recorder, looked at when the drive or what is in it changes.
  const seen = drive ? `${drive.id}|${drive.media}|${drive.label}` : '';
  useEffect(() => {
    if (!drive || !needsDrive || current || working || !resumed || !active) return undefined;
    let alive = true;
    setMedia((now) => (now?.data ? now : { loading: true }));
    api(`/discs/media/${encodeURIComponent(drive.id)}`).then(
      (data) => alive && setMedia({ data }),
      (error) => alive && setMedia({ error }),
    );
    return () => {
      alive = false;
    };
  }, [seen, reads, needsDrive, !current, working, resumed, active]);

  // What would be asked of the server: the same for the check and for the burn.
  const ids = items.map((i) => i.id);
  const gapless = Boolean(choices?.gapless && media?.data?.gapless); // (what was asked for, as far as this recorder and disc can)
  const body = (() => {
    if (!choices || !ids.length) return null;
    if (kind === 'iso') return { track_ids: ids, layout: choices.layout, label: choices.label.trim(), folder: choices.folder.trim() };
    if (!drive) return null;
    if (kind === 'audio') return { drive: drive.id, track_ids: ids, speed: choices.speed, eject: choices.eject, gapless };
    return { drive: drive.id, track_ids: ids, layout: choices.layout, label: choices.label.trim(), speed: choices.speed, eject: choices.eject, verify: choices.verify };
  })();
  const mediaSig = media?.data ? `${media.data.state}|${media.data.type}|${media.data.free_sectors}` : '';
  const key = body ? `${kind}|${mediaSig}|${JSON.stringify(body)}` : '';
  const wanted = useDebounced(key, 450);
  const settled = Boolean(plan && plan.key === key);

  useEffect(() => {
    if (!active || !wanted || working || previewing || current || wanted !== key || (needsDrive && !media?.data) || (plan && plan.key === wanted)) return undefined;
    setPreviewing(true);
    api(`/discs/${ENDPOINTS[kind]}/preview`, { method: 'POST', body }).then(
      (shown) => mounted.current && setPlan({ key: wanted, plan: shown }),
      (error) => mounted.current && setPlan({ key: wanted, error: error.message }),
    ).finally(() => mounted.current && setPreviewing(false));
    return undefined;
  }, [wanted, working, previewing, again, Boolean(media?.data), active]);

  // Songs may have been moved or the disc changed while the window was away: check again when it comes back.
  useEffect(() => {
    const onFocus = () => { if (visible.current && document.visibilityState !== 'hidden') { setReads((n) => n + 1); setPlan(null); setAgain((n) => n + 1); } };
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, []);

  const result = settled ? plan : null;
  const shown = result?.plan;
  const rows = shown ? Object.fromEntries((shown.songs || shown.files || []).map((r) => [r.position, r])) : null;
  const count = shown ? (shown.counts.going ?? shown.songs.length - shown.counts.cannot) : 0;
  const ready = Boolean(shown && shown.ok && count > 0);
  const m = media?.data;

  async function start() {
    const detail = kind === 'audio'
      ? `This writes ${fmt.plural(count, 'song')} (${fmt.duration(shown.disc_seconds)}) to the ${m.type} in drive ${drive.id}, ${gapless ? 'with no gap between the songs' : 'with two seconds of silence between the songs'}. It cannot be undone. If it is stopped or fails once it has begun writing, ${m.rewritable ? 'the disc is unusable until you erase it' : 'a CD-R is spoiled for good'}. A music CD is not read back afterwards, so listen to it once.`
      : kind === 'data'
        ? `This writes ${fmt.plural(count, 'file')} (${fmt.bytes(shown.bytes)}) to the ${m.type} in drive ${drive.id}, and closes the disc. It cannot be undone. If it is stopped or fails once it has begun writing, ${m.rewritable ? 'the disc is unusable until you erase it' : 'the disc is spoiled for good'}. ${choices.verify ? 'Afterwards every file is read back and compared.' : ''}`
        : `This makes a file of ${fmt.bytes(shown.bytes)}: ${shown.output}. Nothing that is there already is replaced.`;
    const ok = await confirmDialog({
      title: kind === 'iso' ? 'Save the disc image?' : kind === 'audio' ? 'Burn this music CD?' : 'Burn this data disc?',
      message: detail,
      confirm: kind === 'iso' ? 'Save the image' : 'Burn the disc',
      danger: kind !== 'iso',
    });
    if (!ok) return;
    try {
      const { job, burn_id: burnId } = await api(`/discs/${ENDPOINTS[kind]}/start`, { method: 'POST', body });
      trackJob(job);
      setCurrent(burnId);
    } catch (error) {
      notifyError(error);
      setPlan(null);
      setAgain((n) => n + 1);
    }
  }

  function another() {
    setCurrent(null);
    setPlan(null);
    setReads((n) => n + 1);
    setAgain((n) => n + 1);
    look(true);
  }

  if (!active) return null; // (kept up to date while another tab is on show, but not drawn)
  if (current) return html`<${BurnProgress} burnId=${current} onNew=${another} />`;
  if ((drives.loading && !drives.data) || !info || !choices) return html`<${Spinner} label="Looking for CD and DVD drives…" />`;
  if (drives.data && !drives.data.available && needsDrive) {
    return html`${kindPicker()}<${Empty} icon="cd" title="Discs are not available here"><p>${drives.data.reason}</p><//>`;
  }

  function kindPicker() {
    return html`<div class="candidate-list" role="radiogroup" aria-label="What to make">
      ${KINDS.map((k) => html`<label class="candidate ${kind === k.id ? 'on' : ''}" key=${k.id}>
        <input type="radio" name="burn-kind" checked=${kind === k.id} onChange=${() => setKind(k.id)} />
        <span class="candidate-main"><strong>${k.label}</strong><span class="subtle">${k.hint}</span></span>
      </label>`)}
    </div>`;
  }

  const trouble = drives.data?.error || (drives.error && drives.error.message);
  return html`
    <p class="hint-card subtle"><${Icon} name="info" size=${14} /> Burning is new and has not been tried on every recorder. Try a rewritable disc (a CD-RW) first. A burn cannot be undone, and if it is stopped or fails once it has begun writing, a CD-R is spoiled for good.</p>
    <h2 class="section-title">What to make</h2>
    ${kindPicker()}

    <h2 class="section-title">Songs</h2>
    <${BurnSongs} items=${items} rows=${rows} kind=${kind} disabled=${false} />
    ${items.length > 0 && html`<div class="row-actions"><${Button} small icon="trash" onClick=${clearBurnList}>Clear the list<//></div>`}

    ${needsDrive && html`
      <h2 class="section-title">The disc</h2>
      ${trouble && html`<div class="callout warn" role="alert"><p><${Icon} name="alert" size=${16} /><span>${trouble}</span></p></div>`}
      ${!drive ? html`<${Empty} icon="cd" title="No disc writer found" level=${3}>
          <p>Music Toolkit looks at the drives Windows lists, and asks Windows' disc burning service which of them can write. If your computer has a writer, plug it in and look again. A disc image (ISO file) needs no drive.</p>
          <${Button} icon="refresh" onClick=${() => look(true)}>Look again<//>
        <//>`
        : html`
        <div class="disc-head">
          <span class="disc-icon"><${Icon} name="cd" size=${26} /></span>
          <div class="disc-head-main">
            ${writers.length > 1
              ? html`<label class="field narrow-inline"><span>Drive</span>
                  <select aria-label="Drive" value=${drive.id} onChange=${(e) => setDriveId(e.target.value)}>
                    ${writers.map((d) => html`<option value=${d.id} key=${d.id}>${d.id} ${d.name}</option>`)}
                  </select></label>`
              : html`<strong>${drive.id} ${drive.name}</strong>`}
            <span class="subtle" role="status">${media?.error ? media.error.message : discWords(m)}</span>
          </div>
          <div class="disc-head-actions">
            <${Button} small icon="refresh" onClick=${() => { setReads((n) => n + 1); look(true); }} title="Look at the disc again">Look again<//>
          </div>
        </div>`}`}

    ${(drive || !needsDrive) && html`
      <h2 class="section-title">How to burn it</h2>
      <div class="organize-form">
        ${needsDrive && m?.speeds?.length > 0 && html`<label class="field"><span>Speed</span>
          <select aria-label="Speed" value=${choices.speed ?? ''} onChange=${(e) => setChoices({ ...choices, speed: e.target.value === '' ? null : Number(e.target.value) })}>
            <option value="">Automatic (the drive chooses)</option>
            ${m.speeds.map((s) => html`<option value=${s.speed} key=${s.speed}>${s.label}${s.speed === m.speeds[0].speed ? ': slowest, gentlest' : ''}</option>`)}
          </select></label>`}
        ${kind === 'audio' && html`<label class="field"><span>Between songs</span>
          <select aria-label="Between songs" value=${gapless ? 'none' : '2s'} onChange=${(e) => setChoices({ ...choices, gapless: e.target.value === 'none' })}>
            <option value="none" disabled=${Boolean(m) && !m.gapless}>No gap: the songs run on from one another${m && !m.gapless ? ' (this recorder cannot)' : ''}</option>
            <option value="2s">Two seconds of silence between the songs</option>
          </select></label>`}
        ${kind !== 'audio' && html`<label class="field"><span>Layout on the disc</span>
          <select aria-label="Layout on the disc" value=${choices.layout} onChange=${(e) => setChoices({ ...choices, layout: e.target.value })}>
            ${LAYOUTS.map(([id, label]) => html`<option value=${id} key=${id}>${label}</option>`)}
          </select></label>
          <label class="field"><span>Name of the disc</span>
            <input type="text" maxlength="16" placeholder="MUSIC" value=${choices.label} onInput=${(e) => setChoices({ ...choices, label: e.target.value })} /></label>`}
        ${kind === 'iso' && html`<label class="field wide"><span>Save the image in this folder</span>
          <input class="mono" type="text" aria-label="Folder for the image" spellcheck="false" value=${choices.folder} onInput=${(e) => setChoices({ ...choices, folder: e.target.value })} /></label>`}
      </div>
      <div class="option-list">
        ${kind === 'data' && html`<label class="check-row"><input type="checkbox" checked=${choices.verify} onChange=${(e) => setChoices({ ...choices, verify: e.target.checked })} /><span>Read every file back from the disc afterwards and compare it (takes about as long as the burn)</span></label>`}
        ${needsDrive && html`<label class="check-row"><input type="checkbox" checked=${choices.eject} onChange=${(e) => setChoices({ ...choices, eject: e.target.checked })} /><span>Eject the disc when it is done</span></label>`}
      </div>

      <h2 class="section-title">Will it fit?</h2>
      ${!items.length ? html`<p class="subtle">Add songs to the list first.</p>`
        : !settled ? html`<p class="status-line" role="status"><span class="spinner small"></span> Checking the list…</p>`
        : result.error ? html`<div class="callout warn" role="alert"><p><${Icon} name="alert" size=${16} /><span>${result.error}</span></p></div>`
        : html`
          ${shown.problems.length > 0 && html`<div class="callout warn" role="alert">${shown.problems.map((p) => html`<p key=${p}><${Icon} name="alert" size=${16} /><span>${p}</span></p>`)}</div>`}
          ${(shown.warnings || []).length > 0 && html`<p class="note" role="status"><${Icon} name="info" size=${14} /><span>${shown.warnings.join(' ')}</span></p>`}
          ${kind !== 'iso' && (kind === 'audio' ? m?.free_sectors : shown.free_sectors) > 0 && html`<div class="capacity-row">
            <progress class="capacity" max=${kind === 'audio' ? m.free_sectors : shown.free_sectors} value=${Math.min(shown.disc_sectors, kind === 'audio' ? m.free_sectors : shown.free_sectors)} aria-label="Room used on the disc"></progress>
            <span class="subtle">${room(shown.disc_sectors, kind)} of ${room(kind === 'audio' ? m.free_sectors : shown.free_sectors, kind)}</span>
          </div>`}
          ${kind === 'iso' && shown.output && html`<p class="subtle">It will be saved as <span class="mono">${shown.output}</span>.</p>`}`}
      <div class="row-actions">
        <${Button} kind="primary" icon="cd" disabled=${!ready || working} onClick=${start}>${kind === 'iso' ? 'Save the disc image…' : kind === 'audio' ? 'Burn the music CD…' : 'Burn the data disc…'}<//>
        <span class="subtle">${ready ? 'You are asked to confirm before anything is written.' : 'Nothing is written until the list has been checked.'}</span>
      </div>

      ${needsDrive && html`<${EraseCard} drive=${drive} media=${m} busy=${working} onStarted=${setCurrent} />`}`}`;
}

// ------------------------------------------------------------ the history

function BurnRow({ burn }) {
  const [open, setOpen] = useState(false);
  const { data: detail } = useAsync(() => (open ? api(`/discs/burns/${burn.id}`) : null), [open, burn.id, burn.status]);
  const good = burn.status === 'done' && !burn.spoiled;
  return html`<li class="rip-row">
    <div class="rip-row-head">
      <button class="chip-btn" type="button" aria-expanded=${open} aria-controls=${`burn-detail-${burn.id}`} onClick=${() => setOpen(!open)}>
        <${Icon} name=${open ? 'chevron-down' : 'chevron-right'} size=${14} /> ${open ? 'Hide' : 'Details'}
      </button>
      <span class="batch-when">${when(burn.started_at)}</span>
      <span class="rip-what"><strong>${burn.title}</strong> <span class="subtle">${[burn.disc_type, burn.drive, burn.kind !== 'erase' ? fmt.plural(burn.items, burn.kind === 'audio' ? 'song' : 'file') : '', burn.kind === 'audio' ? GAPS[burn.gaps] : ''].filter(Boolean).join(' · ')}</span></span>
      <span class="reason ${good ? 'new' : burn.status === 'running' ? '' : 'missing'}">${BURN_STATUS[burn.status] || burn.status}${burn.spoiled && burn.status !== 'done' ? ' · disc spoiled' : ''}${burn.verified === 1 ? ' · checked' : ''}</span>
    </div>
    ${open && html`<div id=${`burn-detail-${burn.id}`} class="rip-detail">
      ${!detail ? html`<${Spinner} />` : html`
        ${detail.message && html`<p class="note"><${Icon} name="info" size=${14} /><span>${detail.message}</span></p>`}
        ${detail.output && html`<p class="subtle mono">${detail.output}</p>`}
        ${detail.tracks.length > 0 && html`<ol class="rip-tracks" aria-label="Songs and files of this burn">${detail.tracks.map((t) => html`<li key=${t.position} class=${t.status}>
          <span class="num">${t.position}</span>
          <span class="rip-song"><strong>${t.title}</strong>${t.message && t.status !== 'waiting' ? html`<span class="subtle">${t.message}</span>` : ''}</span>
          <span class="reason ${CHIP[t.status] || ''}">${ITEM_STATUS[t.status] || t.status}</span>
        </li>`)}</ol>`}`}
    </div>`}
  </li>`;
}

export function BurnHistory({ burns }) {
  if (!burns?.length) return null;
  return html`<h2 class="section-title">Burned</h2>
    <ul class="rip-list" aria-label="Burned discs">${burns.map((burn) => html`<${BurnRow} key=${burn.id} burn=${burn} />`)}</ul>`;
}
