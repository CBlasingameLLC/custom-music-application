// Devices: players, memory cards and drives you copy music to. Choose what goes on one, preview exactly what a
// sync would change, then run it. Taking songs off a device always asks, with the exact count.
import { api, fmt, html, useAsync, useDebounced, useEffect, useRef, useState, useStore } from '../lib.js';
import { Icon } from '../icons.js';
import { Button, Empty, IconButton, JobBanner, PageHeader, Spinner } from '../components.js';
import { confirmDialog, pickFolder, promptText } from '../dialogs.js';
import { Examples } from './organize.js';
import { desktop, go, href, jobs, library, loadPlaylists, notifyError, onJobFinished, openMenu, openModal, toast, trackJob } from '../state.js';

const KINDS = ['sync-preview', 'sync'];
const PAGE = 100;
const ago = (iso) => (iso ? fmt.ago(Date.parse(iso) / 1000) : '');
const roomText = (d) => (d.free != null && d.total ? `${fmt.bytes(d.free)} free of ${fmt.bytes(d.total)}` : '');

const REASONS = {
  new: 'new',
  changed: 'changed',
  moved: 'new location',
  missing: 'gone from device',
  damaged: 'incomplete copy',
};
const SKIPPED = {
  source_missing: 'file not found',
  layout: 'layout problem',
  too_big: 'too big for FAT32',
  path_too_long: 'path too long',
  collision: 'name taken',
};

// ------------------------------------------------------------ choosing what goes on a device

const SOURCE_KINDS = [
  ['favorites', 'Favorites'],
  ['all', 'Whole library'],
  ['playlist', 'A playlist'],
  ['genre', 'A genre'],
  ['rating', 'Songs rated…'],
  ['recent', 'Recently added'],
];
const RECENT = [[7, 'the last 7 days'], [30, 'the last 30 days'], [90, 'the last 3 months'], [365, 'the last year']];

const sourceKey = (s) => `${s.kind}:${s.id ?? s.value ?? s.min ?? s.days ?? ''}`;

function describeSource(s, playlists) {
  switch (s.kind) {
    case 'all': return 'Whole library';
    case 'favorites': return 'Favorites';
    case 'playlist': return `Playlist: ${playlists.find((p) => p.id === s.id)?.name || 'a playlist that was deleted'}`;
    case 'genre': return `Genre: ${s.value}`;
    case 'rating': return `Rated ${s.min}+ stars`;
    default: return `Added in ${RECENT.find(([days]) => days === s.days)?.[1] || `the last ${s.days} days`}`;
  }
}

function SourceEditor({ sources, onChange, disabled }) {
  const playlists = useStore(library, (s) => s.playlists);
  const facets = useStore(library, (s) => s.facets);
  const [kind, setKind] = useState('favorites');
  const [playlistId, setPlaylistId] = useState('');
  const [genre, setGenre] = useState('');
  const [rating, setRating] = useState('4');
  const [days, setDays] = useState('30');
  const genres = facets?.genres || [];
  useEffect(() => { loadPlaylists().catch(() => {}); }, []); // a playlist made a moment ago must be choosable

  const candidate = {
    all: { kind: 'all' },
    favorites: { kind: 'favorites' },
    playlist: playlistId ? { kind: 'playlist', id: Number(playlistId) } : null,
    genre: genre ? { kind: 'genre', value: genre } : null,
    rating: { kind: 'rating', min: Number(rating) },
    recent: { kind: 'recent', days: Number(days) },
  }[kind];
  const duplicate = candidate && sources.some((s) => sourceKey(s) === sourceKey(candidate));

  return html`<div class="source-editor">
    <div class="source-chips" aria-label="What goes on this device">
      ${sources.length === 0 && html`<span class="subtle">Nothing chosen yet. Add the music this device should hold.</span>`}
      ${sources.map((s) => html`<span class="source-chip" key=${sourceKey(s)}>${describeSource(s, playlists)}
        <button type="button" aria-label="Remove ${describeSource(s, playlists)}" disabled=${disabled} onClick=${() => onChange(sources.filter((x) => x !== s))}><${Icon} name="x" size=${13} /></button>
      </span>`)}
    </div>
    <div class="source-add">
      <select aria-label="Kind of music to add" value=${kind} disabled=${disabled} onChange=${(e) => setKind(e.target.value)}>
        ${SOURCE_KINDS.map(([value, label]) => html`<option value=${value} key=${value}>${label}</option>`)}
      </select>
      ${kind === 'playlist' && (playlists.length
        ? html`<select aria-label="Playlist" value=${playlistId} disabled=${disabled} onChange=${(e) => setPlaylistId(e.target.value)}>
            <option value="">Choose a playlist…</option>
            ${playlists.map((p) => html`<option value=${p.id} key=${p.id}>${p.name}${p.kind === 'smart' ? ' (smart)' : ''} · ${fmt.plural(p.tracks, 'song')}</option>`)}
          </select>`
        : html`<span class="subtle">You have no playlists yet.</span>`)}
      ${kind === 'genre' && (genres.length
        ? html`<select aria-label="Genre" value=${genre} disabled=${disabled} onChange=${(e) => setGenre(e.target.value)}>
            <option value="">Choose a genre…</option>
            ${genres.map((g) => html`<option value=${g.name} key=${g.name}>${g.name} · ${fmt.plural(g.count, 'song')}</option>`)}
          </select>`
        : html`<span class="subtle">No genres in the library yet.</span>`)}
      ${kind === 'rating' && html`<select aria-label="Minimum rating" value=${rating} disabled=${disabled} onChange=${(e) => setRating(e.target.value)}>
        ${[5, 4, 3, 2, 1].map((n) => html`<option value=${n} key=${n}>${n} star${n === 1 ? '' : 's'} or more</option>`)}
      </select>`}
      ${kind === 'recent' && html`<select aria-label="How recently added" value=${days} disabled=${disabled} onChange=${(e) => setDays(e.target.value)}>
        ${RECENT.map(([value, label]) => html`<option value=${value} key=${value}>Added in ${label}</option>`)}
      </select>`}
      <${Button} small icon="plus" disabled=${disabled || !candidate || duplicate} onClick=${() => onChange([...sources, candidate])}>Add<//>
    </div>
  </div>`;
}

// ------------------------------------------------------------ adding a device

function PhonesSection({ close, onAdded }) {
  const [looks, setLooks] = useState(0);
  const { data, loading } = useAsync(() => api('/devices/mtp', { params: looks ? { fresh: true } : {} }), [looks]);
  const [folder, setFolder] = useState('Music');
  const [busy, setBusy] = useState(false);

  async function add(device, storage) {
    setBusy(true);
    try {
      const added = await api('/devices/mtp', { method: 'POST', body: { serial: device.serial, storage: storage.id, folder } });
      close();
      onAdded(added);
    } catch (error) {
      notifyError(error);
      setBusy(false);
    }
  }

  const found = (data?.devices || []).flatMap((d) => d.storages.map((s) => ({ device: d, storage: s })));
  const unready = (data?.devices || []).filter((d) => d.storages.length === 0); // plugged in, but showing nothing to copy to
  return html`<section class="phones" aria-label="Phones and players without a drive letter">
    <h3>Phones and players without a drive letter<span class="badge warn">experimental</span></h3>
    ${loading && !data ? html`<${Spinner} />` : data && !data.available
      ? html`<p class="subtle">${data.reason}</p>`
      : html`
        ${data?.error && html`<p class="note warn"><${Icon} name="alert" size=${14} /><span>${data.error}</span></p>`}
        ${!data?.error && (data?.devices || []).length === 0 && html`<p class="subtle">None found. Plug the phone in with a USB cable, unlock its screen, and choose “File transfer” in the notification on it. Then look again.</p>`}
        ${unready.length > 0 && html`<div class="volume-list">
          ${unready.map((device) => html`<div class="volume-row" key=${device.serial}>
            <${Icon} name="phone" size=${20} />
            <div class="volume-main">
              <strong>${device.name}${device.model && device.model !== device.name ? ` (${device.model})` : ''}</strong>
              <span class="subtle">${device.error || 'It is connected but offers nothing to copy to.'} Unlock it, choose “File transfer”, then look again.</span>
            </div>
          </div>`)}
        </div>`}
        ${found.length > 0 && html`<div class="volume-list">
          ${found.map(({ device, storage }) => html`<div class="volume-row" key=${device.serial + storage.id}>
            <${Icon} name="phone" size=${20} />
            <div class="volume-main">
              <strong>${device.name}${device.model && device.model !== device.name ? ` (${device.model})` : ''} · ${storage.name}</strong>
              <span class="subtle">${storage.capacity ? `${fmt.bytes(storage.free)} free of ${fmt.bytes(storage.capacity)}` : ''}</span>
            </div>
            ${storage.device_id
              ? html`<${Button} small onClick=${() => { close(); go(`/devices/${storage.device_id}`); }}>Open<//>`
              : html`<${Button} small kind="primary" disabled=${busy} title="Add ${device.name}, ${storage.name}" onClick=${() => add(device, storage)}>Add<//>`}
          </div>`)}
        </div>`}
        <div class="phones-actions">
          <label class="field"><span>Folder for the music on the phone</span>
            <input type="text" aria-label="Folder for the music on the phone" spellcheck="false" value=${folder} onInput=${(e) => setFolder(e.target.value)} />
          </label>
          <${Button} icon="refresh" disabled=${busy || loading} onClick=${() => setLooks((n) => n + 1)}>Look again<//>
        </div>`}
  </section>`;
}

function AddDeviceBody({ close, onAdded }) {
  const { data, loading } = useAsync(() => api('/devices/volumes'), []);
  const [busy, setBusy] = useState(false);

  async function add(path) {
    setBusy(true);
    try {
      const device = await api('/devices', { method: 'POST', body: { path } });
      close();
      onAdded(device);
    } catch (error) {
      notifyError(error);
      setBusy(false);
    }
  }
  async function browse() {
    const path = await pickFolder('Choose the player, card or folder to copy music to');
    if (path) add(path);
  }

  return html`
    <p class="modal-text">Plug the player or card in, then pick it here. Songs are copied into the folder you choose, and your library is never changed.</p>
    ${loading && !data ? html`<${Spinner} />` : html`<div class="volume-list">
      ${(data?.items || []).map((v) => html`<div class="volume-row" key=${v.mount_path}>
        <${Icon} name="drive" size=${20} />
        <div class="volume-main">
          <strong>${v.label ? `${v.label} (${v.mount_path})` : v.mount_path}</strong>
          <span class="subtle">${[v.fs, v.total ? `${fmt.bytes(v.free)} free of ${fmt.bytes(v.total)}` : ''].filter(Boolean).join(' · ')}</span>
          ${v.music_folder && html`<span class="subtle">It has a Music folder, so songs go into ${v.music_folder}</span>`}
        </div>
        ${v.removable && html`<span class="badge">removable</span>`}
        ${v.holds_library
          ? html`<span class="subtle">Holds your library</span>`
          : v.device_id
            ? html`<${Button} small onClick=${() => { close(); go(`/devices/${v.device_id}`); }}>Open<//>`
            : html`<${Button} small kind="primary" disabled=${busy} title="Add ${v.mount_path}" onClick=${() => add(v.music_folder || v.mount_path)}>Add<//>`}
      </div>`)}
      ${!data?.items?.length && html`<p class="subtle">No drives found. Plug the device in and open this again.</p>`}
    </div>`}
    <div class="modal-actions"><${Button} icon="folder" disabled=${busy} onClick=${browse}>Choose a folder…<//></div>
    <${PhonesSection} close=${close} onAdded=${onAdded} />`;
}

export function openAddDevice(onAdded) {
  openModal({ title: 'Add a device', width: 580, render: (close) => html`<${AddDeviceBody} close=${close} onAdded=${onAdded} />` });
}

// ------------------------------------------------------------ the list of devices

function Capacity({ device }) {
  if (device.free == null || !device.total) return null;
  const used = Math.max(0, Math.min(100, ((device.total - device.free) / device.total) * 100));
  return html`<div class="capacity" role="img" aria-label=${roomText(device)}><div style=${{ width: used + '%' }}></div></div>`;
}

function DeviceCard({ device, onChanged }) {
  async function rename() {
    const label = await promptText({ title: 'Rename device', label: 'Name', value: device.label, confirm: 'Save' });
    if (!label) return;
    try {
      await api(`/devices/${device.id}`, { method: 'PATCH', body: { label } });
      onChanged();
    } catch (error) {
      notifyError(error);
    }
  }
  async function relocate(known) {
    const path = known || await pickFolder(`Where is ${device.label} now?`);
    if (!path) return;
    try {
      await api(`/devices/${device.id}`, { method: 'PATCH', body: { path } });
      toast(`${device.label} is now at ${path}`, 'success');
      onChanged();
    } catch (error) {
      notifyError(error);
    }
  }
  async function forget() {
    const ok = await confirmDialog({
      title: `Forget ${device.label}?`,
      message: 'Music Toolkit stops tracking what it copied there. The songs on the device are left alone, and syncing it again later would copy everything again.',
      confirm: 'Forget device',
      danger: true,
    });
    if (!ok) return;
    try {
      await api(`/devices/${device.id}`, { method: 'DELETE' });
      onChanged();
    } catch (error) {
      notifyError(error);
    }
  }
  const phone = device.kind === 'mtp';
  const menu = (event) => openMenu(event, [
    { icon: 'edit', label: 'Rename', action: rename },
    ...(phone ? [] : [{ icon: 'folder', label: 'Choose its folder again…', action: () => relocate() }]),
    { icon: 'trash', label: 'Forget this device', danger: true, action: forget },
  ]);

  return html`<article class="device-card ${device.connected ? '' : 'offline'}">
    <span class="device-icon"><${Icon} name=${phone ? 'phone' : 'drive'} size=${24} /></span>
    <div class="device-main">
      <h3><a href=${href(`/devices/${device.id}`)}>${device.label}</a>${phone && html`<span class="badge">experimental</span>`}${!device.connected && html`<span class="badge warn">Not connected</span>`}</h3>
      <p class="subtle path">${device.path}${device.volume_label && device.volume_label !== device.label ? ` (${device.volume_label})` : ''}${device.fs ? ` · ${device.fs}` : ''}${device.removable && !phone ? ' · removable' : ''}</p>
      ${device.connected && html`<${Capacity} device=${device} /><p class="subtle">${roomText(device)}</p>`}
      ${device.different_drive && html`<p class="note warn"><${Icon} name="alert" size=${14} /><span>A different drive is using ${device.path} now. Plug the right device in, or choose its folder again.</span></p>`}
      ${!device.connected && !device.different_drive && html`<p class="note warn"><${Icon} name="alert" size=${14} /><span>${phone ? device.note : 'Not connected. Plug it in, or choose its folder again if its drive letter changed.'}</span></p>`}
      ${device.moved_to && html`<p class="note"><${Icon} name="info" size=${14} /><span>It looks like it is on ${device.moved_to} now. <button class="link" onClick=${() => relocate(device.moved_to)}>Use ${device.moved_to}</button></span></p>`}
      <p class="subtle">${device.synced ? `${fmt.plural(device.synced, 'song')} copied (${fmt.bytes(device.synced_bytes)})` : 'Nothing copied yet'}${device.last_synced_at ? ` · last synced ${ago(device.last_synced_at)}` : ''}</p>
    </div>
    <div class="device-actions">
      <${Button} kind="primary" icon="refresh" onClick=${() => go(`/devices/${device.id}`)}>Sync…<//>
      <${IconButton} icon="more" title="More about ${device.label}" onClick=${menu} />
    </div>
  </article>`;
}

export function DevicesView() {
  const [version, setVersion] = useState(0);
  const refresh = () => setVersion((v) => v + 1);
  const { data, error } = useAsync(() => api('/devices'), [version]);
  const { data: volumes } = useAsync(() => api('/devices/volumes').catch(() => null), [version]);
  const [adding, setAdding] = useState(false);
  useEffect(() => {
    window.addEventListener('focus', refresh); // coming back to the window after plugging something in
    return () => window.removeEventListener('focus', refresh);
  }, []);

  const onAdded = (device) => go(`/devices/${device.id}`);
  async function addNow(volume) {
    setAdding(true);
    try {
      onAdded(await api('/devices', { method: 'POST', body: { path: volume.music_folder || volume.mount_path } }));
    } catch (err) {
      notifyError(err);
      setAdding(false);
    }
  }
  const nearby = (volumes?.items || []).filter((v) => v.removable && !v.device_id && !v.holds_library);

  return html`
    <${PageHeader} title="Devices" subtitle="Players, memory cards and drives you copy music to. Songs are copied, never moved.">
      <${Button} icon="refresh" onClick=${refresh} title="Look for drives again">Refresh<//>
      <${Button} kind="primary" icon="plus" onClick=${() => openAddDevice(onAdded)}>Add a device<//>
    <//>
    ${nearby.length > 0 && html`<div class="callout nearby">
      <p><${Icon} name="info" size=${16} /><span>${nearby.length === 1 ? 'This looks like a player or card:' : 'These look like players or cards:'}</span></p>
      ${nearby.map((v) => html`<div class="volume-row" key=${v.mount_path}>
        <${Icon} name="drive" size=${20} />
        <div class="volume-main">
          <strong>${v.label ? `${v.label} (${v.mount_path})` : v.mount_path}</strong>
          <span class="subtle">${[v.fs, v.total ? `${fmt.bytes(v.free)} free of ${fmt.bytes(v.total)}` : ''].filter(Boolean).join(' · ')}${v.music_folder ? ` · songs go into ${v.music_folder}` : ''}</span>
        </div>
        <${Button} small kind="primary" disabled=${adding} title="Use ${v.mount_path}" onClick=${() => addNow(v)}>Use it<//>
      </div>`)}
    </div>`}
    ${error ? html`<p class="subtle">${error.message}</p>` : !data ? html`<${Spinner} />` : data.items.length === 0
      ? html`<${Empty} icon="drive" title="No devices yet">
          <p>Add a Walkman, phone, SD card or USB stick, choose what goes on it, and keep it up to date with one button.</p>
          <${Button} kind="primary" icon="plus" onClick=${() => openAddDevice(onAdded)}>Add a device<//>
        <//>`
      : html`<div class="device-list">${data.items.map((d) => html`<${DeviceCard} key=${d.id} device=${d} onChanged=${refresh} />`)}</div>`}
    <p class="hint-card subtle"><${Icon} name="info" size=${14} /> Only files Music Toolkit copied can be removed from a device, and only after you confirm the exact number. Songs you put there yourself are never touched.</p>`;
}

// ------------------------------------------------------------ syncing one device

function Row({ kind, item }) {
  if (kind === 'copy') {
    return html`<div class="sync-row" role="row">
      <span class="song">${item.title}${item.artist ? html` <span class="subtle">· ${item.artist}</span>` : ''}</span>
      <span class="meta"><span class="reason ${item.reason}">${REASONS[item.reason] || item.reason}</span><span>${fmt.bytes(item.size)}</span></span>
      <span class="where" title=${item.to}>${item.to}</span>
    </div>`;
  }
  if (kind === 'prune') {
    return html`<div class="sync-row" role="row">
      <span class="song">${item.title || item.path}${item.artist ? html` <span class="subtle">· ${item.artist}</span>` : ''}</span>
      <span class="meta">${item.size ? html`<span>${fmt.bytes(item.size)}</span>` : ''}</span>
      <span class="where" title=${item.path}>${item.path}</span>
    </div>`;
  }
  return html`<div class="sync-row" role="row">
    <span class="song">${SKIPPED[item.reason] || item.reason}</span>
    <span class="meta"></span>
    <span class="where" title=${item.source}>${item.source}</span>
    <span class="subtle detail">${item.detail}</span>
  </div>`;
}

const LIST_NAMES = { copy: 'Will copy', prune: 'No longer chosen', skipped: 'Left out' };

/** One sentence for what a finished sync did. */
function outcomeText(o) {
  const parts = [];
  if (o.copied) parts.push(`Copied ${fmt.plural(o.copied, 'song')} (${fmt.bytes(o.bytes_copied)}) to ${o.label}`);
  if (o.pruned) parts.push(o.copied ? `removed ${fmt.plural(o.pruned, 'song')}` : `Removed ${fmt.plural(o.pruned, 'song')} from ${o.label}`);
  if (o.playlists_written) parts.push(`wrote ${fmt.plural(o.playlists_written, 'playlist')}`);
  let text = parts.length ? `${parts.join(', ')}.` : 'Nothing was copied.';
  if (o.aborted) text += ` It stopped early: ${o.aborted}`;
  if (o.errors_total) text += ` ${fmt.plural(o.errors_total, 'song')} could not be copied; the next sync tries again.`;
  return text;
}

export function DeviceView({ id }) {
  const { items: jobItems } = useStore(jobs);
  const [version, setVersion] = useState(0);
  const { data: device, error } = useAsync(() => api(`/devices/${id}`), [id, version]);
  const { data: layouts } = useAsync(() => api('/tools/organize/info').catch(() => null), []);
  const [form, setForm] = useState(null); // { sources, scheme, playlists, covers }, once the device has loaded
  const [plan, setPlan] = useState(null); // { jobId, key, result, lists: { copy: {items,total}, ... } }
  const [list, setList] = useState('copy');
  const [pending, setPending] = useState(null); // the preview job we are waiting for
  const [prune, setPrune] = useState(false);
  const [outcome, setOutcome] = useState(null);
  const pendingRef = useRef(null);
  const previewKey = useRef('');
  pendingRef.current = pending;
  const typed = useDebounced(form?.scheme || '', 250);
  const { data: example } = useAsync(() => (typed.trim() ? api('/tools/organize/example', { params: { scheme: typed } }) : null), [typed]);

  const busy = jobItems.some((j) => KINDS.includes(j.kind) && (j.status === 'running' || j.status === 'queued'));
  const keyOf = (f) => JSON.stringify([f.sources.map(sourceKey), f.scheme.trim(), f.playlists, f.covers]);

  useEffect(() => {
    if (device && !form) {
      setForm({
        sources: device.prefs.sources,
        scheme: device.prefs.scheme || device.default_scheme || '',
        playlists: device.prefs.playlists,
        covers: device.prefs.covers,
      });
    }
  }, [device]);

  useEffect(() => onJobFinished((job) => {
    if (job.kind === 'sync-preview' && job.id === pendingRef.current) {
      setPending(null);
      if (job.status === 'done') loadPlan(job);
    } else if (job.kind === 'sync') {
      setVersion((v) => v + 1);
      setPlan(null);
      setPrune(false);
      if (job.status === 'done' && job.result && String(job.result.device_id) === String(id)) setOutcome(job.result);
      else if (job.status === 'cancelled') toast('Stopped. Songs already copied are on the device, and the next sync carries on from there.', 'info', 9000);
    }
  }), []);

  async function loadPlan(job) {
    try {
      const result = job.result;
      const first = result.to_copy ? 'copy' : result.to_prune ? 'prune' : 'skipped';
      const lists = {};
      if (result.to_copy || result.to_prune || result.skipped) {
        lists[first] = await api(`/devices/${id}/preview/${job.id}`, { params: { kind: first, limit: PAGE } });
      }
      setPlan({ jobId: job.id, key: previewKey.current, result, lists });
      setList(first);
    } catch (err) {
      notifyError(err);
    }
  }

  async function showList(kind) {
    setList(kind);
    if (!plan || plan.lists[kind]) return;
    try {
      const page = await api(`/devices/${id}/preview/${plan.jobId}`, { params: { kind, limit: PAGE } });
      setPlan((p) => p && { ...p, lists: { ...p.lists, [kind]: page } });
    } catch (err) {
      notifyError(err);
    }
  }

  async function more() {
    const have = plan.lists[list];
    try {
      const page = await api(`/devices/${id}/preview/${plan.jobId}`, { params: { kind: list, offset: have.items.length, limit: PAGE } });
      setPlan((p) => p && { ...p, lists: { ...p.lists, [list]: { ...page, items: [...have.items, ...page.items] } } });
    } catch (err) {
      notifyError(err);
    }
  }

  async function runPreview() {
    setOutcome(null);
    setPlan(null);
    setPrune(false);
    const scheme = form.scheme.trim();
    previewKey.current = keyOf(form);
    try {
      const { job } = await api(`/devices/${id}/preview`, {
        method: 'POST',
        body: { sources: form.sources, scheme: !scheme || scheme === device.default_scheme ? null : scheme, playlists: form.playlists, covers: form.covers },
      });
      pendingRef.current = job.id; // before the job can finish, not after the next render
      setPending(job.id);
      trackJob(job);
    } catch (err) {
      notifyError(err);
    }
  }

  async function startSync() {
    const r = plan.result;
    const removing = prune && r.to_prune > 0;
    if (removing) {
      const ok = await confirmDialog({
        title: `Remove ${fmt.plural(r.to_prune, 'song')} from ${device.label}?`,
        message: `These songs were copied by Music Toolkit earlier but are no longer part of what you chose. They will be deleted from the device, which frees ${fmt.bytes(r.prune_bytes)}. Your library is not touched.`,
        confirm: `Remove ${fmt.plural(r.to_prune, 'song')}`,
        danger: true,
      });
      if (!ok) return;
    }
    try {
      const { job } = await api(`/devices/${id}/sync`, { method: 'POST', body: { job_id: plan.jobId, prune: removing, confirm_prune: removing ? r.to_prune : null } });
      setOutcome(null);
      trackJob(job);
    } catch (err) {
      notifyError(err);
    }
  }

  if (error) return html`<${PageHeader} title="Device" /><p class="subtle">${error.message} <a href=${href('/devices')}>Back to devices</a></p>`;
  if (!device || !form) return html`<${PageHeader} title="Device" /><${Spinner} />`;

  const phone = device.kind === 'mtp';
  const presets = layouts?.presets || [];
  const layoutChoice = form.scheme.trim() === (device.default_scheme || '') ? '' : presets.find((p) => p.scheme === form.scheme.trim())?.scheme ?? '__custom';
  const current = plan && plan.key === keyOf(form);
  const stale = plan && !current;
  const r = plan?.result;
  const removing = Boolean(r && prune && r.to_prune > 0);
  const fits = r && (removing ? r.enough_space_with_removals : r.enough_space);
  const nothingToDo = r && r.to_copy === 0 && !removing;
  const shown = plan?.lists[list];
  const hasPlaylistSource = form.sources.some((s) => s.kind === 'playlist');
  const lists = r ? [['copy', r.to_copy], ['prune', r.to_prune], ['skipped', r.skipped]].filter(([, n]) => n > 0) : [];

  return html`
    <${PageHeader} title=${device.label} subtitle=${phone ? `${device.volume_label ? `${device.volume_label} · ` : ''}${device.path}` : `${device.path}${device.fs ? ` · ${device.fs}` : ''}`}>
      <${Button} icon="chevron-left" onClick=${() => go('/devices')}>All devices<//>
      ${desktop?.openPath && device.connected && !phone && html`<${Button} icon="folder" onClick=${() => desktop.openPath(device.path)}>Open folder<//>`}
    <//>
    ${phone && html`<div class="callout" role="note"><p><${Icon} name="info" size=${16} /><span>Copying to phones and players without a drive letter is new, and has not been tried on every model. Start with a small choice (one playlist, say) and check that it plays. Keep the phone unlocked while it copies.</span></p></div>`}
    <div class="device-status">
      <${Capacity} device=${device} />
      <span class="subtle">${device.connected ? roomText(device) : 'Not connected'}${device.synced ? ` · ${fmt.plural(device.synced, 'song')} copied (${fmt.bytes(device.synced_bytes)})` : ''}${device.last_synced_at ? ` · last synced ${ago(device.last_synced_at)}` : ''}</span>
    </div>
    ${!device.connected && html`<div class="callout warn" role="alert"><p><${Icon} name="alert" size=${16} /><span>${phone ? device.note : html`${device.different_drive ? `A different drive is using ${device.path} now.` : 'This device is not connected.'}${device.moved_to ? ` It looks like it is on ${device.moved_to} now.` : ''} Plug it in, or go back and choose its folder again.`}</span></p></div>`}

    <h2 class="section-title">What goes on this device</h2>
    <${SourceEditor} sources=${form.sources} disabled=${busy} onChange=${(sources) => setForm((f) => ({ ...f, sources }))} />

    <h2 class="section-title">How it is laid out</h2>
    <div class="organize-form">
      <label class="field">
        <span>Folder layout on the device</span>
        <select aria-label="Folder layout on the device" value=${layoutChoice} disabled=${busy} onChange=${(e) => {
          if (e.target.value === '') setForm((f) => ({ ...f, scheme: device.default_scheme || '' }));
          else if (e.target.value !== '__custom') setForm((f) => ({ ...f, scheme: e.target.value }));
        }}>
          <option value="">Standard layout</option>
          ${presets.map((p) => html`<option value=${p.scheme} key=${p.scheme}>${p.name}</option>`)}
          <option value="__custom">Custom</option>
        </select>
      </label>
      <label class="field wide">
        <span>Folder and file name, built from these fields</span>
        <input class="mono" type="text" spellcheck="false" aria-label="Layout pattern" value=${form.scheme} disabled=${busy} onInput=${(e) => { const scheme = e.target.value; setForm((f) => ({ ...f, scheme })); }} />
      </label>
      <${Examples} example=${example} />
    </div>
    <div class="option-list">
      <label class="check-row"><input type="checkbox" checked=${form.covers} disabled=${busy} onChange=${(e) => { const covers = e.target.checked; setForm((f) => ({ ...f, covers })); }} /><span>Copy folder pictures (cover.jpg) along with the songs</span></label>
      <label class="check-row"><input type="checkbox" checked=${form.playlists} disabled=${busy || !hasPlaylistSource} onChange=${(e) => { const playlists = e.target.checked; setForm((f) => ({ ...f, playlists })); }} /><span>Also write each chosen playlist to the device as an .m3u8 file${hasPlaylistSource ? '' : ' (choose a playlist above to use this)'}</span></label>
    </div>
    <div class="row-actions">
      <${Button} kind="primary" icon="search" disabled=${busy || !device.connected || form.sources.length === 0 || example?.ok === false} onClick=${runPreview}>Preview what will be copied<//>
      <span class="subtle">Nothing is written to the device until you have seen the preview.</span>
    </div>
    <${JobBanner} kinds=${KINDS} />

    ${outcome && html`<div class="callout ${outcome.errors_total || outcome.aborted ? 'warn' : ''}" role="status">
      <p><${Icon} name=${outcome.errors_total || outcome.aborted ? 'alert' : 'check'} size=${16} /><span>${outcomeText(outcome)}</span></p>
      ${outcome.errors?.length > 0 && html`<ul class="problem-list">${outcome.errors.slice(0, 20).map((e) => html`<li key=${e.path}><code>${e.path}</code> <span class="subtle">${e.error}</span></li>`)}</ul>`}
    </div>`}

    ${stale && html`<div class="callout"><p><${Icon} name="info" size=${16} /><span>You changed the choices after previewing. Preview again to see what would happen now.</span></p></div>`}

    ${current && r && html`
      <div class="stat-row">
        <div class="stat"><strong>${fmt.number(r.selected)}</strong><span>${r.selected === 1 ? 'song' : 'songs'} chosen</span></div>
        <div class="stat"><strong>${fmt.number(r.to_copy)}</strong><span>to copy · ${fmt.bytes(r.bytes)}</span></div>
        <div class="stat"><strong>${fmt.number(r.unchanged)}</strong><span>already on the device</span></div>
        ${r.to_prune > 0 && html`<div class="stat"><strong>${fmt.number(r.to_prune)}</strong><span>on it, no longer chosen · ${fmt.bytes(r.prune_bytes)}</span></div>`}
        ${r.skipped > 0 && html`<div class="stat"><strong>${fmt.number(r.skipped)}</strong><span>left out</span></div>`}
      </div>
      ${r.to_copy > 0 && Object.keys(r.reasons).length > 0 && html`<p class="subtle reasons">${Object.entries(r.reasons).map(([reason, n]) => `${fmt.number(n)} ${REASONS[reason] || reason}`).join(' · ')}</p>`}
      ${!r.enough_space && html`<div class="callout warn" role="alert"><p><${Icon} name="alert" size=${16} /><span>
        Not enough room on the device: ${fmt.bytes(r.bytes)} to copy, ${fmt.bytes(r.free)} free.
        ${r.enough_space_with_removals ? ` Removing the ${fmt.plural(r.to_prune, 'song')} that are no longer chosen would make room.` : ' Choose less music, or free some space on the device.'}</span></p></div>`}
      ${r.to_prune > 0 && html`<label class="check-row prune-row"><input type="checkbox" checked=${prune} disabled=${busy} onChange=${(e) => setPrune(e.target.checked)} />
        <span>Also remove the ${fmt.plural(r.to_prune, 'song')} that are no longer chosen from the device (${fmt.bytes(r.prune_bytes)}). Only songs this app copied are ever removed.</span></label>`}
      ${nothingToDo
        ? html`<${Empty} icon="check" title=${r.to_prune ? 'Nothing new to copy' : 'The device is up to date'}>
            <p>${r.to_prune ? 'Everything chosen is already on the device. Tick the box above to remove what is no longer chosen.' : 'Everything you chose is already on the device.'}</p>
          <//>`
        : html`<div class="organize-apply">
            <${Button} kind="primary" icon="check" disabled=${busy || !fits} onClick=${startSync}>${r.to_copy > 0
              ? `Copy ${fmt.plural(r.to_copy, 'song')} (${fmt.bytes(r.bytes)})${removing ? ` and remove ${fmt.plural(r.to_prune, 'song')}` : ''}`
              : `Remove ${fmt.plural(r.to_prune, 'song')} from the device`}<//>
            <span class="subtle">${removing ? 'The removal is confirmed first.' : 'Nothing on the device is removed.'} You can stop at any time and carry on later.</span>
          </div>`}
      <div class="tabs" role="tablist">
        ${lists.map(([kind, n]) => html`<button role="tab" key=${kind} aria-selected=${list === kind} class="tab ${list === kind ? 'active' : ''}" onClick=${() => showList(kind)}>${LIST_NAMES[kind]} <span class="count">${fmt.number(n)}</span></button>`)}
      </div>
      ${shown && html`<div class="sync-table" role="table" aria-label=${LIST_NAMES[list]}>
        ${shown.items.map((item, i) => html`<${Row} key=${i} kind=${list} item=${item} />`)}
      </div>
      ${shown.total > shown.items.length && html`<p class="more-row"><${Button} onClick=${more}>Show more (${fmt.number(shown.total - shown.items.length)} left)<//></p>`}`}`}

    <p class="hint-card subtle"><${Icon} name="info" size=${14} /> ${phone
      ? 'Songs are copied, never moved. A phone cannot rename a file, so a copy that is cut short is deleted again, and anything that still looks too small next time is copied again. Some players do not show .m3u8 playlists in their own playlist screen. Your choices are remembered for this device.'
      : 'Songs are copied, never moved, and each file is written under a temporary name and renamed when complete, so unplugging mid-copy never leaves half a song. Your choices are remembered for this device.'}</p>`;
}
