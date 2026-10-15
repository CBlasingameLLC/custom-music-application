// Discs: copy an audio CD into the library as FLAC or MP3. Look at the disc, find the album, see where every song would go, rip it,
// watch it happen, keep the history, take a rip back. (Burning comes next to this.)
import { api, fmt, html, useAsync, useCallback, useDebounced, useEffect, useLayoutEffect, useRef, useState, useStore } from '../lib.js';
import { Icon } from '../icons.js';
import { Button, Empty, JobBanner, PageHeader, Spinner, TabPanel, Tabs } from '../components.js';
import { confirmDialog } from '../dialogs.js';
import { Examples } from './organize.js';
import { go, href, jobs, notifyError, onJobFinished, trackJob } from '../state.js';

const WATCH_MS = 3000; // how often the drives are looked at while this page is open
const looked = new Map(); // disc id -> the albums MusicBrainz offered for it, so that coming back to the page does not ask again

const MEDIA = {
  none: 'No disc',
  loading: 'Reading the disc…',
  audio: 'Audio CD',
  mixed: 'Audio CD',
  data: 'Data disc',
  unknown: 'A disc this drive cannot read',
};
const RIP_STATUS = {
  running: 'Ripping',
  done: 'Done',
  partial: 'Partly done',
  cancelled: 'Stopped',
  failed: 'Failed',
  interrupted: 'Interrupted',
};
const TRACK_STATUS = {
  waiting: 'Waiting',
  ripping: 'Ripping',
  done: 'Done',
  problem: 'Done, with trouble',
  skipped: 'Left out',
  failed: 'Not ripped',
};
const PLAN_ACTION = {
  rip: 'Will rip',
  skip: 'Left out',
  exists: 'Already there',
  clash: 'Same name',
  too_long: 'Name too long',
  problem: 'Cannot name',
};
// The chip colours of the sync lists, reused: green for done, amber for trouble, grey for the rest.
const CHIP = { done: 'new', problem: 'missing', failed: 'damaged', rip: 'new', exists: 'missing', clash: 'damaged', too_long: 'damaged' };

const when = (iso) => {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : `${date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}, ${date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}`;
};
const minutes = (seconds) => `${Math.max(1, Math.round(seconds / 60))} min`;
/** A refusal of the form's contents arrives as a list of what is wrong, for programs; say it in a sentence. */
const friendly = (message) => (String(message).startsWith('[') ? 'Something in the details above cannot be used (a year, a disc number or a title that is too long).' : message);

// ------------------------------------------------------------ looking at the drives

/** The CD and DVD drives and what is in them, looked at again every few seconds while `watching`. */
function useDrives(watching) {
  const [state, setState] = useState({ loading: true, data: null, error: null });
  const look = useCallback(async (fresh) => {
    try {
      const data = await api('/discs/drives', { params: fresh ? { fresh: true } : {} });
      setState((before) => {
        // Looking failed this time (the helper did not answer): what was seen before stays on the screen, with the trouble above it.
        if (data.error && !data.drives.length && before.data?.drives.length) return { ...before, loading: false, error: new Error(data.error) };
        return JSON.stringify(before.data) === JSON.stringify(data) && !before.error ? before : { loading: false, data, error: null };
      });
    } catch (error) {
      setState((before) => ({ ...before, loading: false, error }));
    }
  }, []);
  useEffect(() => {
    if (!watching) return undefined;
    look(true);
    const timer = setInterval(() => !document.hidden && look(false), WATCH_MS);
    const onFocus = () => look(true); // coming back to the window after putting a disc in
    window.addEventListener('focus', onFocus);
    return () => {
      clearInterval(timer);
      window.removeEventListener('focus', onFocus);
    };
  }, [watching, look]);
  return [state, look];
}

// ------------------------------------------------------------ choosing the album

function candidateLine(c) {
  return [c.artist, c.year, c.country, c.label, c.disc_total > 1 ? `disc ${c.disc_number} of ${c.disc_total}` : '', fmt.plural(c.tracks.length, 'song')].filter(Boolean).join(' · ');
}

function Albums({ lookup, candidates, picked, onPick, contactSet, attachUrl }) {
  if (!contactSet) {
    return html`<p class="note"><${Icon} name="info" size=${14} /><span>To find this album on MusicBrainz, add your email address under Accounts and services in <a href=${href('/settings')}>Settings</a> (MusicBrainz asks every app to say who is asking). Or type the album in by hand below.</span></p>`;
  }
  if (lookup.state === 'working') return html`<p class="status-line" role="status"><span class="spinner small"></span> Looking the disc up on MusicBrainz…</p>`;
  return html`
    ${lookup.state === 'error' && html`<div class="callout warn" role="alert"><p><${Icon} name="alert" size=${16} /><span>${lookup.message}</span></p></div>`}
    ${lookup.state === 'none' && html`<p class="note" role="status"><${Icon} name="info" size=${14} /><span>MusicBrainz does not know this disc. Type the album in below.${attachUrl ? html` If you add it to MusicBrainz, the next person who rips it will not have to. <a href=${attachUrl} target="_blank" rel="noopener noreferrer">Add it <${Icon} name="external" size=${12} /></a>` : ''}</span></p>`}
    ${candidates.length > 0 && html`<div class="candidate-list" role="radiogroup" aria-label="Which album is this disc?">
      ${candidates.map((c, i) => html`<label class="candidate ${picked === i ? 'on' : ''}" key=${(c.release_id || '') + i}>
        <input type="radio" name="album-choice" checked=${picked === i} onChange=${() => onPick(i)} />
        <span class="candidate-main">
          <strong>${c.title}</strong>${c.exact ? html`<span class="badge">this very disc</span>` : html`<span class="badge warn">similar disc</span>`}
          <span class="subtle">${candidateLine(c)}</span>
        </span>
      </label>`)}
      <label class="candidate ${picked === -1 ? 'on' : ''}">
        <input type="radio" name="album-choice" checked=${picked === -1} onChange=${() => onPick(-1)} />
        <span class="candidate-main"><strong>None of these</strong><span class="subtle">I will type the album in myself</span></span>
      </label>
    </div>`}
    <div class="row-actions">
      <${Button} small icon="refresh" disabled=${lookup.state === 'working'} onClick=${() => onPick('again')}>Ask MusicBrainz again<//>
      <span class="subtle">Looking it up sends this disc's table of contents (how long each song is) to musicbrainz.org, and nothing else.</span>
    </div>`;
}

// ------------------------------------------------------------ the songs

function SongTable({ form, lengths, disabled, onChange }) {
  const all = form.tracks.every((t) => t.rip);
  const none = form.tracks.every((t) => !t.rip);
  const set = (number, patch) => onChange({ ...form, tracks: form.tracks.map((t) => (t.number === number ? { ...t, ...patch } : t)) });
  return html`<div class="rip-table-wrap"><table class="rip-table">
    <caption class="sr-only">Songs on the disc</caption>
    <thead><tr>
      <th scope="col" class="rip-check"><input type="checkbox" aria-label="Rip every song" checked=${all} indeterminate=${!all && !none} disabled=${disabled}
        onChange=${(e) => onChange({ ...form, tracks: form.tracks.map((t) => ({ ...t, rip: e.target.checked })) })} /></th>
      <th scope="col" class="rip-num">#</th>
      <th scope="col">Title</th>
      ${form.variousArtists && html`<th scope="col">Artist</th>`}
      <th scope="col" class="rip-time">Length</th>
    </tr></thead>
    <tbody>${form.tracks.map((t) => html`<tr key=${t.number} class=${t.rip ? '' : 'off'}>
      <td class="rip-check"><input type="checkbox" aria-label="Rip song ${t.number}" checked=${t.rip} disabled=${disabled} onChange=${(e) => set(t.number, { rip: e.target.checked })} /></td>
      <td class="rip-num">${t.number}</td>
      <td><input type="text" aria-label="Title of song ${t.number}" value=${t.title} disabled=${disabled} onInput=${(e) => set(t.number, { title: e.target.value })} /></td>
      ${form.variousArtists && html`<td><input type="text" aria-label="Artist of song ${t.number}" value=${t.artist} disabled=${disabled} onInput=${(e) => set(t.number, { artist: e.target.value })} /></td>`}
      <td class="rip-time">${lengths[t.number] != null ? fmt.time(lengths[t.number]) : ''}</td>
    </tr>`)}</tbody>
  </table></div>`;
}

// ------------------------------------------------------------ what a rip would do

function Plan({ plan, checking }) {
  if (!plan) return checking ? html`<p class="status-line" role="status"><span class="spinner small"></span> Working out where the songs will go…</p>` : null;
  if (plan.error) return html`<div class="callout warn" role="alert"><p><${Icon} name="alert" size=${16} /><span>${plan.error}</span></p></div>`;
  const p = plan.plan;
  const counts = p.counts;
  return html`
    ${p.problems.length > 0 && html`<div class="callout warn" role="alert">
      ${p.problems.map((text) => html`<p key=${text}><${Icon} name="alert" size=${16} /><span>${text}</span></p>`)}
    </div>`}
    <div class="stat-row">
      <div class="stat"><strong>${fmt.number(counts.rip)}</strong><span>${counts.rip === 1 ? 'song' : 'songs'} will be ripped</span></div>
      <div class="stat"><strong>${fmt.bytes(p.estimated_bytes)}</strong><span>about, ${p.free_bytes != null ? `${fmt.bytes(p.free_bytes)} free` : 'free space not known'}</span></div>
      ${counts.skip > 0 && html`<div class="stat"><strong>${fmt.number(counts.skip)}</strong><span>left out</span></div>`}
      ${counts.exists > 0 && html`<div class="stat"><strong>${fmt.number(counts.exists)}</strong><span>already there, left alone</span></div>`}
      ${counts.clash + counts.too_long + counts.problem > 0 && html`<div class="stat"><strong>${fmt.number(counts.clash + counts.too_long + counts.problem)}</strong><span>cannot be named</span></div>`}
    </div>
    <div class="sync-table" role="table" aria-label="Where the songs will go">
      ${p.tracks.map((t) => html`<div class="sync-row" role="row" key=${t.number}>
        <span class="song" role="cell">${t.number}. ${t.title}</span>
        <span class="meta" role="cell"><span class="reason ${CHIP[t.action] || ''}">${PLAN_ACTION[t.action] || t.action}</span><span>${fmt.time(t.seconds)}</span></span>
        <span class="where" role="cell" title=${t.path || ''}>${t.path || ''}</span>
        ${t.note && t.action !== 'skip' && html`<span class="subtle detail" role="cell">${t.note}</span>`}
      </div>`)}
    </div>`;
}

// ------------------------------------------------------------ a rip under way, or just finished

function describeRip(rip) {
  const songs = rip.tracks.filter((t) => t.status === 'done' || t.status === 'problem').length;
  const notRipped = rip.tracks.filter((t) => t.status === 'failed' || (t.status === 'skipped' && t.message === 'Not reached.')).length;
  const wanted = songs + notRipped;
  if (rip.status === 'running') return null;
  if (rip.status === 'done') {
    return `Ripped ${fmt.plural(songs, 'song')} to ${rip.folder || rip.root}.${rip.problems ? ` ${fmt.plural(rip.problems, 'part')} of the audio could not be read perfectly; the songs concerned are marked below.` : ' Every song was read twice and the two readings agreed.'}`;
  }
  if (rip.status === 'partial') return `Ripped ${fmt.plural(songs, 'song')} of ${wanted}. ${rip.message || ''}`.trim();
  if (rip.status === 'cancelled') return `Stopped. ${songs ? `${fmt.plural(songs, 'song')} had been ripped, and ${songs === 1 ? 'is' : 'are'} in your library.` : 'Nothing was ripped.'}`;
  return rip.message || 'The rip did not finish.';
}

function RipProgress({ ripId, onNew }) {
  const [rip, setRip] = useState(null);
  const heading = useRef();
  const { items } = useStore(jobs);
  const job = items.find((j) => j.kind === 'rip' && (j.status === 'running' || j.status === 'queued'));
  const undoing = items.find((j) => j.kind === 'rip-undo');
  const running = rip ? rip.status === 'running' : true;
  useLayoutEffect(() => heading.current?.focus({ preventScroll: true }), []);
  useEffect(() => {
    let alive = true;
    let timer = null;
    const fetchIt = async () => {
      try {
        const detail = await api(`/discs/rips/${ripId}`);
        if (alive) setRip(detail);
        if (detail.status !== 'running') clearInterval(timer); // (it is looked at again when something happens to the rip: see the list below)
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
  }, [ripId, job?.status, undoing?.status]);
  const words = rip && describeRip(rip);
  const undoable = rip && !running && !rip.undone_at && rip.tracks.some((t) => t.status === 'done' || t.status === 'problem');

  async function undo() {
    const songs = rip.tracks.filter((t) => t.status === 'done' || t.status === 'problem').length;
    const ok = await confirmDialog({
      title: `Take back this rip?`,
      message: `The ${fmt.plural(songs, 'song')} this rip made go to the Recycle Bin and leave your library. A song you have edited since is left alone. The disc itself is not touched.`,
      confirm: `Move ${fmt.plural(songs, 'song')} to the Recycle Bin`,
      danger: true,
    });
    if (!ok) return;
    try {
      const { job: started } = await api(`/discs/rips/${ripId}/undo`, { method: 'POST' });
      trackJob(started);
    } catch (error) {
      notifyError(error);
    }
  }

  return html`<section aria-labelledby="rip-title">
    <h2 id="rip-title" tabIndex="-1" ref=${heading}>${rip ? `${rip.album}${rip.artist ? ` · ${rip.artist}` : ''}` : 'Ripping'}</h2>
    <${JobBanner} kinds=${['rip']} />
    ${rip && !running && html`<div class="callout ${rip.status === 'done' && !rip.problems ? '' : 'warn'}" role="status">
      <p><${Icon} name=${rip.status === 'done' && !rip.problems ? 'check' : 'alert'} size=${16} /><span>${rip.undone_at ? 'This rip has been taken back: its songs went to the Recycle Bin.' : words}</span></p>
    </div>`}
    ${rip && html`<ol class="rip-tracks" aria-label="Songs">
      ${rip.tracks.map((t) => html`<li key=${t.number} class=${t.status}>
        <span class="num">${t.number}</span>
        <span class="rip-song"><strong>${t.title}</strong>${t.message && t.status !== 'skipped' ? html`<span class="subtle">${t.message}</span>` : ''}</span>
        <span class="reason ${CHIP[t.status] || ''}">${t.status === 'ripping' ? html`<span class="spinner small"></span>` : ''} ${TRACK_STATUS[t.status] || t.status}</span>
      </li>`)}
    </ol>`}
    ${rip && !running && html`<div class="row-actions">
      ${rip.status !== 'failed' && rip.tracks.some((t) => t.status === 'done' || t.status === 'problem') && !rip.undone_at && html`<${Button} icon="music" onClick=${() => go(`/search/${encodeURIComponent(rip.album)}`)}>Show in my library<//>`}
      ${undoable && html`<${Button} icon="undo" onClick=${undo}>Take this rip back<//>`}
      <${Button} kind="primary" icon="cd" onClick=${onNew}>Rip another disc<//>
    </div>`}
  </section>`;
}

// ------------------------------------------------------------ ripping

const emptyForm = { album: '', album_artist: '', year: '', date: '', genre: '', disc_number: 1, disc_total: 1, release_id: null, release_group_id: null, artist_ids: [], barcode: '', label: '', tracks: [], variousArtists: false };

function formFrom(candidate) {
  return {
    album: candidate.title, album_artist: candidate.artist, year: candidate.year ?? '', date: candidate.date || '', genre: '',
    disc_number: candidate.disc_number || 1, disc_total: candidate.disc_total || 1, release_id: candidate.release_id, release_group_id: candidate.release_group_id,
    artist_ids: candidate.artist_ids || [], barcode: candidate.barcode || '', label: candidate.label || '',
    tracks: candidate.tracks.map((t) => ({ number: t.number, title: t.title, artist: t.artist || '', recording_id: t.recording_id || null, rip: true })),
    variousArtists: candidate.tracks.some((t) => t.artist && t.artist !== candidate.artist),
  };
}

function RipTab({ active }) {
  const { items: jobItems } = useStore(jobs);
  const running = jobItems.some((j) => j.kind === 'rip' && (j.status === 'running' || j.status === 'queued'));
  const { data: info } = useAsync(() => api('/discs/options'), []);
  const [drives, look] = useDrives(active && !running);
  const [driveId, setDriveId] = useState('');
  const [disc, setDisc] = useState(null); // null | { loading } | { error } | { data }
  const [reads, setReads] = useState(0);
  const [lookup, setLookup] = useState({ state: 'idle' }); // idle | working | done | none | error
  const [candidates, setCandidates] = useState([]);
  const [picked, setPicked] = useState(-1);
  const [form, setForm] = useState(emptyForm);
  const [choices, setChoices] = useState(null); // format, quality, accuracy, root, scheme, cover, eject
  const [plan, setPlan] = useState(null); // { key, plan } or { key, error }
  const [current, setCurrent] = useState(null); // the rip being watched: its id
  const [previewing, setPreviewing] = useState(false); // one preview at a time, so that the last one the server holds is the last one asked for
  const [again, setAgain] = useState(0);
  const [resumed, setResumed] = useState(false); // has it been looked for yet: a rip that is under way
  const edited = useRef(false);
  const startedFor = useRef(null); // the disc the form was started from: the same disc read again does not start it over
  const askedFor = useRef(null); // the disc MusicBrainz was asked about
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);
  const lookupJob = useRef(null);
  const discRef = useRef(null);
  discRef.current = disc?.data;

  // The drive the person chose, while it is there; otherwise the first with music in it, otherwise the first.
  const list = drives.data?.drives || [];
  const drive = list.find((d) => d.id === driveId) || list.find((d) => d.media === 'audio' || d.media === 'mixed') || list[0];
  const seen = drive && (drive.media === 'audio' || drive.media === 'mixed') ? `${drive.id}|${drive.label}` : '';

  // The defaults from Settings, once.
  useEffect(() => {
    if (!info || choices) return;
    setChoices({
      format: info.defaults.format, quality: info.defaults.quality, mp3_quality: info.defaults.mp3_quality, accuracy: info.defaults.accuracy,
      root: info.defaults.root, scheme: info.scheme, cover: info.defaults.cover, eject: info.defaults.eject,
    });
  }, [info]);

  // A rip that is still going (the page was left and came back, or reloaded) is picked up again.
  useEffect(() => {
    api('/discs/rips').then(
      ({ rips }) => {
        if (rips[0] && rips[0].status === 'running') setCurrent((now) => now ?? rips[0].id);
        setResumed(true);
      },
      () => setResumed(true),
    );
  }, []);

  // Read the table of contents when a music disc is in the drive.
  const busy = Boolean(drives.data?.busy); // a rip has the drive: it is not asked anything else
  useEffect(() => {
    if (!seen || !drive || current || busy || !resumed) {
      if (!seen) setDisc(null);
      return undefined;
    }
    let alive = true;
    setDisc((now) => (now?.data ? now : { loading: true }));
    api(`/discs/drives/${encodeURIComponent(drive.id)}/disc`).then(
      (data) => alive && setDisc({ data }),
      (error) => alive && setDisc({ error }),
    );
    return () => {
      alive = false;
    };
  }, [seen, reads, !current, busy, resumed]);

  // An empty tray: the next disc, even the same one, starts from the beginning.
  const tray = drive?.media;
  useEffect(() => {
    if (!drive || tray === 'none') {
      startedFor.current = null;
      askedFor.current = null;
    }
  }, [tray, Boolean(drive)]);

  // A different disc: start from what is known about it. (The same disc read again, because the drive was busy a moment or a look at
  // the drives failed once, keeps what has been typed.)
  const discId = disc?.data?.disc_id;
  useEffect(() => {
    const data = discRef.current;
    if (!data || startedFor.current === data.disc_id) return;
    startedFor.current = data.disc_id;
    edited.current = false;
    const cached = looked.get(data.disc_id);
    setPlan(null);
    if (cached && cached.length) {
      setCandidates(cached);
      setPicked(0);
      setForm(formFrom(cached[0]));
      setLookup({ state: 'done' });
    } else {
      setCandidates([]);
      setPicked(-1);
      setForm(formFrom(data.manual));
      setLookup({ state: cached ? 'none' : 'idle' });
    }
  }, [discId]);

  // ... and ask MusicBrainz about it, once, as soon as there is a contact to ask with (the answer may arrive after the disc was read).
  useEffect(() => {
    const data = discRef.current;
    if (!data || !info?.contact_set || askedFor.current === data.disc_id || looked.has(data.disc_id)) return;
    askedFor.current = data.disc_id;
    askMusicBrainz(data.drive);
  }, [discId, info?.contact_set]);

  async function askMusicBrainz(driveLetter) {
    setLookup({ state: 'working' });
    try {
      const { job } = await api('/discs/lookup', { method: 'POST', body: { drive: driveLetter } });
      lookupJob.current = job.id;
      trackJob(job);
    } catch (error) {
      setLookup({ state: 'error', message: error.message });
    }
  }

  useEffect(() => onJobFinished((job) => {
    if (job.kind !== 'disc-lookup' || job.id !== lookupJob.current) return;
    lookupJob.current = null;
    if (job.status !== 'done') {
      setLookup({ state: 'error', message: job.status === 'error' ? job.error : 'Stopped.' });
      return;
    }
    const found = job.result.candidates;
    looked.set(job.result.disc_id, found);
    if (discRef.current?.disc_id !== job.result.disc_id) return;
    setCandidates(found);
    if (!found.length) {
      setLookup({ state: 'none' });
      return;
    }
    setLookup({ state: 'done' });
    if (!edited.current) {
      setPicked(0);
      setForm(formFrom(found[0]));
    }
  }), []);

  function pick(choice) {
    if (choice === 'again') {
      looked.delete(discRef.current?.disc_id);
      askMusicBrainz(discRef.current.drive);
      return;
    }
    edited.current = false;
    setPicked(choice);
    setForm(formFrom(choice === -1 ? discRef.current.manual : candidates[choice]));
  }

  function change(next) {
    edited.current = true;
    setForm(next);
  }
  const field = (name) => (e) => change({ ...form, [name]: e.target.value });

  // What would be asked of the server; the same for the preview and for the rip.
  const body = disc?.data && choices && drive ? {
    drive: drive.id, root: choices.root, scheme: choices.scheme.trim(), format: choices.format,
    quality: choices.format === 'mp3' ? choices.mp3_quality : 'standard', accuracy: choices.accuracy,
    album: form.album.trim(), album_artist: form.album_artist.trim(), year: form.year === '' ? null : Number(form.year), date: form.date || '',
    genre: form.genre.trim(), disc_number: Number(form.disc_number) || 1, disc_total: Number(form.disc_total) || 1,
    release_id: form.release_id, release_group_id: form.release_group_id, artist_ids: form.artist_ids, barcode: form.barcode, label: form.label,
    tracks: form.tracks.map((t) => ({ number: t.number, title: t.title.trim(), artist: form.variousArtists ? t.artist.trim() : '', recording_id: t.recording_id, rip: t.rip })),
    cover: choices.cover, eject: choices.eject,
  } : null;
  const key = body ? JSON.stringify(body) : '';
  const wanted = useDebounced(key, 450);
  const ripping = Boolean(current) || running;

  useEffect(() => {
    if (!wanted || ripping || previewing || wanted !== key || !form.tracks.length || (plan && plan.key === wanted)) return undefined;
    const asked = JSON.parse(wanted);
    if (!asked.scheme) {
      setPlan({ key: wanted, error: 'Type a layout: how the folders and the files are named.' });
      return undefined;
    }
    setPreviewing(true);
    api('/discs/rip/preview', { method: 'POST', body: asked }).then(
      (shown) => mounted.current && setPlan({ key: wanted, plan: shown }),
      (error) => mounted.current && setPlan({ key: wanted, error: friendly(error.message) }),
    ).finally(() => mounted.current && setPreviewing(false));
    return undefined;
  }, [wanted, ripping, previewing, again]);

  // A file may have been moved or made while the window was away (in File Explorer, say): look at where the songs will go again.
  useEffect(() => {
    const onFocus = () => {
      setPlan(null);
      setAgain((n) => n + 1);
    };
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, []);

  const settled = plan && plan.key === key;
  const ready = settled && plan.plan && plan.plan.ok;
  const count = settled && plan.plan ? plan.plan.counts.rip : 0;
  const waiting = lookup.state === 'working'; // what the album is called is about to arrive: ripping now would use the names typed so far

  async function start() {
    try {
      const started = await api('/discs/rip/start', { method: 'POST', body: JSON.parse(key) });
      trackJob(started.job);
      setPlan(null); // the server's preview is used up by this rip
      setCurrent(started.rip_id);
    } catch (error) {
      notifyError(error);
      setPlan(null); // the preview is used up or out of date: look again
      setAgain((n) => n + 1);
    }
  }

  async function eject() {
    try {
      await api('/discs/eject', { method: 'POST', body: { drive: drive.id } });
      look(true);
    } catch (error) {
      notifyError(error);
    }
  }

  const roots = info?.roots || [];
  const lengths = Object.fromEntries((disc?.data?.tracks || []).map((t) => [t.number, t.seconds]));
  const totalSeconds = Object.values(lengths).reduce((a, b) => a + b, 0);

  // ---- what to show

  if (current) {
    return html`<${RipProgress} key=${current} ripId=${current} onNew=${() => { document.getElementById('main')?.focus({ preventScroll: true }); setCurrent(null); setReads((n) => n + 1); look(true); }} />`;
  }
  if ((drives.loading && !drives.data) || !info || !choices) return html`<${Spinner} label="Looking for CD and DVD drives…" />`;
  if (drives.data && !drives.data.available) {
    return html`<${Empty} icon="cd" title="Discs are not available here"><p>${drives.data.reason}</p><//>`;
  }
  const trouble = drives.data?.error || (drives.error && drives.error.message);
  if (!list.length) {
    return html`${trouble && html`<div class="callout warn" role="alert"><p><${Icon} name="alert" size=${16} /><span>${trouble}</span></p></div>`}
      <${Empty} icon="cd" title="No CD or DVD drive found">
        <p>Music Toolkit looks at the drives Windows lists. If this computer has no disc drive, a USB one will do: plug it in and look again.</p>
        <${Button} icon="refresh" onClick=${() => look(true)}>Look again<//>
      <//>`;
  }

  const media = drive?.media;
  return html`
    <div class="disc-head">
      <span class="disc-icon"><${Icon} name="cd" size=${26} /></span>
      <div class="disc-head-main">
        ${list.length > 1
          ? html`<label class="field narrow-inline"><span>Drive</span>
              <select aria-label="Drive" value=${drive.id} onChange=${(e) => setDriveId(e.target.value)}>
                ${list.map((d) => html`<option value=${d.id} key=${d.id}>${d.id} ${d.name}: ${MEDIA[d.media] || d.media}</option>`)}
              </select></label>`
          : html`<strong>${drive.id} ${drive.name}</strong>`}
        <span class="subtle">${MEDIA[media] || media}${disc?.data ? ` · ${fmt.plural(disc.data.tracks.length, 'song')} · ${minutes(totalSeconds)}` : ''}${drive.label && media !== 'audio' && media !== 'mixed' && media !== 'none' ? ` · ${drive.label}` : ''}</span>
      </div>
      <div class="disc-head-actions">
        <${Button} small icon="refresh" onClick=${() => { setReads((n) => n + 1); look(true); }} title="Look at the drive again">Read the disc again<//>
        ${media && media !== 'none' && media !== 'loading' && html`<${Button} small icon="upload" onClick=${eject}>Eject<//>`}
      </div>
    </div>
    ${trouble && html`<div class="callout warn" role="alert"><p><${Icon} name="alert" size=${16} /><span>${trouble}</span></p></div>`}

    ${media === 'none' && html`<${Empty} icon="cd" title="Put an audio CD in the drive">
      <p>This page notices when a disc goes in. The songs are copied into your library as FLAC or MP3, with their names, tags and cover.</p>
    <//>`}
    ${media === 'loading' && html`<${Spinner} label="The drive is reading the disc…" />`}
    ${media === 'data' && html`<${Empty} icon="cd" title="This is a data disc, not a music CD"><p>${drive.label ? `“${drive.label}” holds files, not songs.` : 'It holds files, not songs.'} To copy files from it, use File Explorer.</p><//>`}
    ${media === 'unknown' && html`<${Empty} icon="cd" title="This drive cannot read the disc">
      <p>It may be blank, scratched, dirty, or not a music CD. Take it out, wipe it, and put it back in.</p>
    <//>`}

    ${(media === 'audio' || media === 'mixed') && html`
      ${disc?.error && html`<div class="callout warn" role="alert"><p><${Icon} name="alert" size=${16} /><span>${disc.error.message}</span></p></div>`}
      ${!disc?.data && !disc?.error && html`<${Spinner} label="Reading the disc…" />`}
      ${disc?.data && html`
        <h2 class="section-title" id="which-album">Which album is it?</h2>
        <${Albums} lookup=${lookup} candidates=${candidates} picked=${picked} onPick=${pick} contactSet=${info.contact_set} attachUrl=${disc.data.attach_url} />

        <h2 class="section-title">Album details</h2>
        <div class="organize-form">
          <label class="field"><span>Album artist</span><input type="text" value=${form.album_artist} onInput=${field('album_artist')} /></label>
          <label class="field"><span>Album</span><input type="text" value=${form.album} onInput=${field('album')} /></label>
          <label class="field"><span>Year</span><input type="number" min="1" max="9999" value=${form.year} onInput=${field('year')} /></label>
          <label class="field"><span>Genre</span><input type="text" value=${form.genre} onInput=${field('genre')} /></label>
          <label class="field"><span>Disc number</span><input type="number" min="1" max="99" value=${form.disc_number} onInput=${field('disc_number')} /></label>
          <label class="field"><span>Discs in the set</span><input type="number" min="1" max="99" value=${form.disc_total} onInput=${field('disc_total')} /></label>
        </div>

        <h2 class="section-title">Songs</h2>
        <${SongTable} form=${form} lengths=${lengths} disabled=${false} onChange=${change} />
        <label class="check-row"><input type="checkbox" checked=${form.variousArtists} onChange=${(e) => change({ ...form, variousArtists: e.target.checked })} /><span>The songs have different artists (a compilation)</span></label>

        <h2 class="section-title">How to rip it</h2>
        <div class="organize-form">
          <label class="field">
            <span>Format</span>
            <select aria-label="Format" value=${choices.format} onChange=${(e) => setChoices({ ...choices, format: e.target.value })}>
              <option value="flac">FLAC: exactly the disc, about half the size</option>
              <option value="mp3">MP3: small files that play anywhere</option>
            </select>
          </label>
          ${choices.format === 'mp3'
            ? html`<label class="field"><span>MP3 quality</span>
                <select aria-label="MP3 quality" value=${choices.mp3_quality} onChange=${(e) => setChoices({ ...choices, mp3_quality: e.target.value })}>
                  ${info.qualities.mp3.map((q) => html`<option value=${q} key=${q}>${q} kbps${q === '320' ? ': the best' : q === '128' ? ': smallest' : ''}</option>`)}
                </select></label>`
            : html`<label class="field"><span>Accuracy</span>
                <select aria-label="Accuracy" value=${choices.accuracy} onChange=${(e) => setChoices({ ...choices, accuracy: e.target.value })}>
                  <option value="verified">Verified: read twice and compare (slower, safest)</option>
                  <option value="fast">Fast: read once</option>
                </select></label>`}
          ${choices.format === 'mp3' && html`<label class="field">
            <span>Accuracy</span>
            <select aria-label="Accuracy" value=${choices.accuracy} onChange=${(e) => setChoices({ ...choices, accuracy: e.target.value })}>
              <option value="verified">Verified: read twice and compare (slower, safest)</option>
              <option value="fast">Fast: read once</option>
            </select></label>`}
          <label class="field">
            <span>Where in your library</span>
            <select aria-label="Library folder" value=${choices.root} onChange=${(e) => setChoices({ ...choices, root: e.target.value })}>
              ${roots.map((r) => html`<option value=${r.path} key=${r.path}>${r.path}${r.available ? '' : ' (not connected)'}</option>`)}
            </select>
          </label>
          <label class="field">
            <span>Layout</span>
            <select aria-label="Layout preset" value=${info.presets.find((p) => p.scheme === choices.scheme)?.scheme || ''} onChange=${(e) => e.target.value && setChoices({ ...choices, scheme: e.target.value })}>
              <option value="">Custom</option>
              ${info.presets.map((p) => html`<option value=${p.scheme} key=${p.scheme}>${p.name}</option>`)}
            </select>
          </label>
          <label class="field wide">
            <span>Folder and file name, built from these fields</span>
            <input class="mono" type="text" aria-label="Layout pattern" spellcheck="false" value=${choices.scheme} onInput=${(e) => setChoices({ ...choices, scheme: e.target.value })} />
          </label>
          <${LayoutExample} scheme=${choices.scheme} />
        </div>
        ${!roots.length && html`<div class="callout warn" role="alert"><p><${Icon} name="alert" size=${16} /><span>There is no music folder to rip into yet. Add one in <a href=${href('/settings')}>Settings</a>.</span></p></div>`}
        <div class="option-list">
          <label class="check-row"><input type="checkbox" checked=${choices.cover} onChange=${(e) => setChoices({ ...choices, cover: e.target.checked })} /><span>Put the album's cover picture in the songs and in the folder${info.contact_set ? '' : ' (needs the album to be found on MusicBrainz)'}</span></label>
          <label class="check-row"><input type="checkbox" checked=${choices.eject} onChange=${(e) => setChoices({ ...choices, eject: e.target.checked })} /><span>Eject the disc when it is done, unless a part of it could not be read perfectly</span></label>
        </div>

        <h2 class="section-title">Where the songs will go</h2>
        <${Plan} plan=${settled ? plan : null} checking=${!settled} />
        <div class="row-actions">
          <${Button} kind="primary" icon="cd" disabled=${!ready || waiting} onClick=${start}>${count ? `Rip ${fmt.plural(count, 'song')}` : 'Rip'}<//>
          <span class="subtle">${waiting ? 'Waiting for MusicBrainz to say which album this is…' : ready ? 'Nothing is overwritten: a name that is taken is left alone.' : 'Nothing is written until this shows where the songs will go.'}</span>
        </div>
        <p class="hint-card subtle"><${Icon} name="info" size=${14} /> Ripping is new and has not been tried on every drive. Start with one disc and listen to it. Every song is read twice and compared; a part the drive could not give back the same way twice is marked in the history instead of being passed off as good.</p>`}`}`;
}

/** The layout, as it would name a song (from the same sample the Organize tool uses). */
function LayoutExample({ scheme }) {
  const typed = useDebounced(scheme, 250);
  const { data: example } = useAsync(() => (typed.trim() ? api('/tools/organize/example', { params: { scheme: typed } }) : null), [typed]);
  return html`<${Examples} example=${example} />`;
}

// ------------------------------------------------------------ the history

function RipRow({ rip, busy, onUndo }) {
  const [open, setOpen] = useState(false);
  const { data: detail } = useAsync(() => (open ? api(`/discs/rips/${rip.id}`) : null), [open, rip.id, rip.status, rip.undone_at]);
  const undoable = rip.status !== 'running' && !rip.undone_at && rip.songs > 0;
  const quality = rip.format === 'mp3' ? `MP3 ${rip.quality} kbps` : 'FLAC';
  return html`<li class="rip-row">
    <div class="rip-row-head">
      <button class="chip-btn" type="button" aria-expanded=${open} aria-controls=${`rip-detail-${rip.id}`} onClick=${() => setOpen(!open)}>
        <${Icon} name=${open ? 'chevron-down' : 'chevron-right'} size=${14} /> ${open ? 'Hide' : 'Details'}
      </button>
      <span class="batch-when">${when(rip.started_at)}</span>
      <span class="rip-what"><strong>${rip.album}</strong> <span class="subtle">${rip.artist} · ${quality} · ${rip.songs} of ${rip.tracks} songs</span></span>
      <span class="reason ${rip.status === 'done' && !rip.problems ? 'new' : rip.status === 'running' ? '' : 'missing'}">${rip.undone_at ? 'Taken back' : RIP_STATUS[rip.status] || rip.status}${rip.problems && !rip.undone_at ? ` · ${fmt.plural(rip.problems, 'doubtful part')}` : ''}</span>
      ${undoable && html`<${Button} small icon="undo" disabled=${busy} onClick=${() => onUndo(rip)}>Take back<//>`}
    </div>
    ${open && html`<div id=${`rip-detail-${rip.id}`} class="rip-detail">
      ${!detail ? html`<${Spinner} />` : html`
        ${detail.message && html`<p class="note"><${Icon} name="info" size=${14} /><span>${detail.message}</span></p>`}
        <p class="subtle">${detail.folder || detail.root}${detail.disc_id ? ` · disc ${detail.disc_id}` : ''}</p>
        <ol class="rip-tracks" aria-label="Songs of this rip">${detail.tracks.map((t) => html`<li key=${t.number} class=${t.status}>
          <span class="num">${t.number}</span>
          <span class="rip-song"><strong>${t.title}</strong>${t.message && t.status !== 'skipped' ? html`<span class="subtle">${t.message}</span>` : ''}</span>
          <span class="reason ${CHIP[t.status] || ''}">${TRACK_STATUS[t.status] || t.status}</span>
        </li>`)}</ol>`}
    </div>`}
  </li>`;
}

function HistoryTab() {
  const { items: jobItems } = useStore(jobs);
  const [version, setVersion] = useState(0);
  const { data, error } = useAsync(() => api('/discs/rips'), [version]);
  const busy = jobItems.some((j) => j.kind === 'rip-undo' && (j.status === 'running' || j.status === 'queued'));
  useEffect(() => onJobFinished((job) => (job.kind === 'rip' || job.kind === 'rip-undo') && setVersion((v) => v + 1)), []);

  async function undo(rip) {
    const ok = await confirmDialog({
      title: 'Take back this rip?',
      message: `The ${fmt.plural(rip.songs, 'song')} of “${rip.album}” go to the Recycle Bin and leave your library. A song you have edited since is left alone.`,
      confirm: `Move ${fmt.plural(rip.songs, 'song')} to the Recycle Bin`,
      danger: true,
    });
    if (!ok) return;
    try {
      const { job } = await api(`/discs/rips/${rip.id}/undo`, { method: 'POST' });
      trackJob(job);
    } catch (err) {
      notifyError(err);
    }
  }

  if (error) return html`<p class="subtle">${error.message}</p>`;
  if (!data) return html`<${Spinner} />`;
  if (!data.rips.length) {
    return html`<${Empty} icon="cd" title="Nothing ripped yet"><p>Every disc you rip is listed here, with what happened to each song, so that you can take it back.</p><//>`;
  }
  return html`<${JobBanner} kinds=${['rip-undo']} />
    <ul class="rip-list">${data.rips.map((rip) => html`<${RipRow} key=${rip.id} rip=${rip} busy=${busy} onUndo=${undo} />`)}</ul>`;
}

// ------------------------------------------------------------ the page

export function DiscsView() {
  const [tab, setTab] = useState('rip');
  return html`
    <${PageHeader} title="Discs" subtitle="Copy an audio CD into your library as FLAC or MP3, with the right names, tags and cover. Experimental: it works with the drive's own interface, and has been tried on stand-in drives, not yet on every real one." />
    <${Tabs} id="discs" label="Discs" value=${tab} onChange=${setTab}
      tabs=${[{ id: 'rip', label: 'Rip a CD', icon: 'cd' }, { id: 'history', label: 'History', icon: 'clock' }]} />
    <${TabPanel} id="discs" value=${tab}>
      <div hidden=${tab !== 'rip'}><${RipTab} active=${tab === 'rip'} /></div>
      ${tab === 'history' && html`<${HistoryTab} />`}
    <//>`;
}
