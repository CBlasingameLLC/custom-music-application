// Settings: music folders, accounts, folder layouts, appearance, data.
import { api, fmt, html, useEffect, useState, useStore } from '../lib.js';
import { Icon } from '../icons.js';
import { Button, PageHeader, Spinner } from '../components.js';
import { addMusicFolder, confirmDialog } from '../dialogs.js';
import { bumpLibrary, desktop, library, loadAbout, loadSettings, notifyError, openModal, setTheme, toast, trackJob, ui, updates } from '../state.js';

function Card({ icon, title, children, hint }) {
  return html`<section class="card-panel">
    <h2><${Icon} name=${icon} size=${18} /> ${title}</h2>
    ${hint && html`<p class="subtle">${hint}</p>`}
    ${children}
  </section>`;
}

function Switch({ checked, onChange, children }) {
  return html`<label class="switch"><input type="checkbox" checked=${checked} onChange=${(e) => onChange(e.target.checked)} /><span>${children}</span></label>`;
}

async function save(patch, message = 'Saved') {
  try {
    const settings = await api('/settings', { method: 'PUT', body: patch });
    library.set((s) => ({ ...s, settings }));
    toast(message, 'success', 2200);
    return settings;
  } catch (error) {
    notifyError(error);
    return null;
  }
}

function LibraryCard({ settings, about }) {
  async function remove(path) {
    const ok = await confirmDialog({
      title: 'Remove music folder',
      message: `Stop showing the music in "${path}"? Your files are not touched or deleted. You can add the folder back at any time.`,
      confirm: 'Remove', danger: true,
    });
    if (!ok) return;
    try {
      const result = await api('/library/roots/remove', { method: 'POST', body: { path } });
      toast(`Removed. ${fmt.plural(result.hidden_tracks, 'song')} hidden from the library.`);
      await loadSettings(); bumpLibrary(); loadAbout();
    } catch (error) { notifyError(error); }
  }
  async function rescan() {
    try { const { job } = await api('/library/scan', { method: 'POST' }); trackJob(job); toast('Looking for new and changed music…'); } catch (error) { notifyError(error); }
  }
  const roots = settings.library.roots;
  return html`<${Card} icon="folder" title="Music library" hint="Music Toolkit reads the tags of the files in these folders. It never changes your files unless you use a tool that says so.">
    ${roots.length === 0 && html`<p class="subtle">No folders yet.</p>`}
    <ul class="root-list">${roots.map((path) => html`<li key=${path}>
      <${Icon} name="folder" size=${16} /><span class="path">${path}</span>
      <button class="icon-btn" aria-label="Remove ${path}" onClick=${() => remove(path)}><${Icon} name="trash" size=${16} /></button></li>`)}</ul>
    <div class="row-actions">
      <${Button} kind="primary" icon="plus" onClick=${addMusicFolder}>Add music folder<//>
      <${Button} icon="refresh" disabled=${!roots.length} onClick=${rescan}>Scan for new music<//>
      <span class="subtle">${about ? fmt.plural(about.tracks, 'song') + ' in the library' : ''}</span>
    </div>
    <${Switch} checked=${settings.app.rescan_on_launch} onChange=${(v) => save({ app: { rescan_on_launch: v } })}>Look for new music every time the app starts<//>
  <//>`;
}

/** Tries the saved key or token against its service. `before` saves anything typed but not yet saved. */
function TestButton({ service, before }) {
  const [result, setResult] = useState(null); // null | 'working' | { ok, message }
  async function run() {
    setResult('working');
    if (before && !(await before())) return setResult(null);
    try {
      setResult(await api(`/settings/test/${service}`, { method: 'POST' }));
    } catch (error) {
      setResult({ ok: false, message: error.message });
    }
  }
  return html`<${Button} icon="link" disabled=${result === 'working'} onClick=${run}>${result === 'working' ? 'Testing…' : 'Test connection'}<//>
    ${result && result !== 'working' && html`<span class="test-result ${result.ok ? 'ok' : 'bad'}" role="status"><${Icon} name=${result.ok ? 'check' : 'alert'} size=${14} /><span>${result.message}</span></span>`}`;
}

/** Whether plays are reaching ListenBrainz: up to date, how many are waiting, or what went wrong. */
function ScrobbleStatus() {
  const [status, setStatus] = useState(null);
  const load = () => api('/scrobbler').then(setStatus).catch(() => {});
  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);
  async function retry() {
    try {
      setStatus(await api('/scrobbler/retry', { method: 'POST' }));
    } catch (error) {
      notifyError(error);
    }
  }
  if (!status || !status.has_token) return null;
  const wait = status.next_attempt_at ? Math.max(0, Math.round(status.next_attempt_at - Date.now() / 1000)) : 0;
  const line = !status.active
    ? 'Not sending: ListenBrainz or recording plays is switched off above.'
    : status.state === 'rejected'
      ? status.last_error
      : status.state === 'waiting'
        ? `${status.last_error} Trying again in ${wait < 90 ? fmt.plural(wait, 'second') : fmt.plural(Math.round(wait / 60), 'minute')}.`
        : status.pending
          ? `${fmt.plural(status.pending, 'play')} waiting to be sent.`
          : `Up to date.${status.sent ? ` ${fmt.plural(status.sent, 'play')} sent since the app started.` : ''}`;
  const refused = status.refused
    ? ` ${fmt.plural(status.refused, 'play')} that ListenBrainz refused ${status.refused === 1 ? 'was' : 'were'} skipped${status.last_refused ? ` (latest: “${status.last_refused}”)` : ''}.`
    : '';
  return html`<p class="status-line ${status.active ? status.state : 'off'}" role="status" aria-label="Scrobbling status">${line}${refused}
    ${status.active && (status.state === 'rejected' || status.state === 'waiting') && html` <button class="link" onClick=${retry}>Try again</button>`}</p>`;
}

function AccountsCard({ settings }) {
  const [mb, setMb] = useState(settings.musicbrainz.contact);
  const [lbUser, setLbUser] = useState(settings.listenbrainz.username);
  const [lbToken, setLbToken] = useState('');
  const [lfKey, setLfKey] = useState('');
  const [lfSecret, setLfSecret] = useState('');
  const lb = settings.listenbrainz;
  const lf = settings.lastfm;

  const saveMusicBrainz = async () => mb === settings.musicbrainz.contact || Boolean(await save({ musicbrainz: { contact: mb } }));
  const saveListenBrainz = async () => {
    const patch = {};
    if (lbUser !== lb.username) patch.username = lbUser;
    if (lbToken) patch.user_token = lbToken;
    if (!Object.keys(patch).length) return true;
    const saved = await save({ listenbrainz: patch });
    if (saved) setLbToken('');
    return Boolean(saved);
  };
  const saveLastFm = async () => {
    const patch = {};
    if (lfKey) patch.api_key = lfKey;
    if (lfSecret) patch.api_secret = lfSecret;
    if (!Object.keys(patch).length) return true;
    const saved = await save({ lastfm: patch });
    if (saved) { setLfKey(''); setLfSecret(''); }
    return Boolean(saved);
  };

  return html`<${Card} icon="globe" title="Accounts and services" hint="All optional and free. Nothing is sent anywhere until you turn it on. “Test connection” saves what you typed and tries it.">
    <div class="service">
      <h3>MusicBrainz <span class="badge">tag lookups</span></h3>
      <p class="subtle">MusicBrainz asks apps to identify themselves with a contact address. Used when filling in missing song tags.</p>
      <div class="inline-form"><input type="email" aria-label="MusicBrainz contact email" placeholder="you@example.com" value=${mb} onInput=${(e) => setMb(e.target.value)} />
        <${Button} onClick=${() => save({ musicbrainz: { contact: mb } })}>Save<//></div>
      <div class="row-actions"><${TestButton} service="musicbrainz" before=${saveMusicBrainz} /></div>
    </div>
    <div class="service">
      <h3>ListenBrainz <span class="badge">recommendations and listening history</span></h3>
      <p class="subtle">A free, non-profit home for your listening history. Create an account at listenbrainz.org, then copy your user token from its profile page.</p>
      <${Switch} checked=${lb.enabled} onChange=${(v) => save({ listenbrainz: { enabled: v } })}>Use ListenBrainz<//>
      <div class="form-grid">
        <label class="field"><span>Username</span><input type="text" value=${lbUser} placeholder="your ListenBrainz username" onInput=${(e) => setLbUser(e.target.value)} /></label>
        <label class="field"><span>User token ${lb.has_token ? html`<em class="ok">saved</em>` : ''}</span>
          <input type="password" value=${lbToken} placeholder=${lb.has_token ? 'Enter a new token to replace it' : 'Paste your token'} autocomplete="off" onInput=${(e) => setLbToken(e.target.value)} /></label>
      </div>
      <${Switch} checked=${lb.scrobble} onChange=${(v) => save({ listenbrainz: { scrobble: v } })}>Record what I play in this app to ListenBrainz<//>
      <${Switch} checked=${lb.now_playing} onChange=${(v) => save({ listenbrainz: { now_playing: v } })}>Also show what I'm playing right now (shown for a few minutes, never kept as a listen)<//>
      <${ScrobbleStatus} />
      <div class="row-actions">
        <${Button} onClick=${saveListenBrainz}>Save ListenBrainz<//>
        <${TestButton} service="listenbrainz" before=${saveListenBrainz} />
        ${lb.has_token && html`<${Button} kind="danger-ghost" onClick=${() => save({ listenbrainz: { user_token: '' } }, 'Token removed')}>Remove token<//>`}
      </div>
    </div>
    <div class="service">
      <h3>Last.fm <span class="badge">more recommendations</span></h3>
      <p class="subtle">Optional extra source of similar-artist suggestions. Create a free API account at last.fm/api.</p>
      <${Switch} checked=${lf.enabled} onChange=${(v) => save({ lastfm: { enabled: v } })}>Use Last.fm<//>
      <div class="form-grid">
        <label class="field"><span>API key ${lf.has_key ? html`<em class="ok">saved</em>` : ''}</span><input type="password" value=${lfKey} placeholder=${lf.has_key ? 'Enter a new key to replace it' : 'API key'} autocomplete="off" onInput=${(e) => setLfKey(e.target.value)} /></label>
        <label class="field"><span>Shared secret ${lf.has_secret ? html`<em class="ok">saved</em>` : ''}</span><input type="password" value=${lfSecret} placeholder=${lf.has_secret ? 'Enter a new secret to replace it' : 'Shared secret'} autocomplete="off" onInput=${(e) => setLfSecret(e.target.value)} /></label>
      </div>
      <div class="row-actions">
        <${Button} onClick=${saveLastFm}>Save Last.fm<//>
        <${TestButton} service="lastfm" before=${saveLastFm} />
      </div>
    </div>
  <//>`;
}

function LayoutsCard({ settings }) {
  const [organize, setOrganize] = useState(settings.library.canonical_scheme);
  const [device, setDevice] = useState(settings.sync.device_scheme);
  return html`<${Card} icon="layers" title="Folder layouts" hint="How files are named when you tidy the library or copy music to a player. Fields: {album_artist} {artist} {album} {title} {track:02d} {disc} {year} {ext}. Use Library tools, Organize files to preview and apply it.">
    <label class="field"><span>Tidy up the library as</span><input type="text" value=${organize} onInput=${(e) => setOrganize(e.target.value)} /></label>
    <label class="field"><span>Copy to players as</span><input type="text" value=${device} onInput=${(e) => setDevice(e.target.value)} /></label>
    <${Button} onClick=${() => save({ library: { canonical_scheme: organize }, sync: { device_scheme: device } })}>Save layouts<//>
  <//>`;
}

function UpdatesCard({ settings }) {
  const { status } = useStore(updates);
  if (!desktop?.update || !status) return null;
  const busy = ['checking', 'downloading', 'ready'].includes(status.state);
  const line = {
    idle: 'Not checked yet.',
    checking: 'Checking for a newer version…',
    'up-to-date': `You have the latest version (${status.current}).${status.checkedAt ? ` Checked ${fmt.ago(status.checkedAt / 1000)}.` : ''}`,
    downloading: `Downloading version ${status.version}… ${status.percent}%`,
    ready: `Version ${status.version} is ready. It installs when you close the app, or now if you restart.`,
    error: status.error || 'The last check did not work.',
    disabled: 'Updates only work in the installed app.',
  }[status.state] || '';
  return html`<${Card} icon="download" title="Updates" hint="New versions come from this project's page on GitHub. They download in the background and install when you close the app. Nothing is sent but the request for the version file.">
    <p class="status-line ${status.state}" role="status">${line}</p>
    ${status.state === 'downloading' && html`<div class="progress"><div style=${{ width: status.percent + '%' }}></div></div>`}
    <${Switch} checked=${settings.app.auto_update} onChange=${async (on) => { if (await save({ app: { auto_update: on } })) desktop.update.setAuto(on); }}>Look for updates automatically<//>
    <div class="row-actions">
      <${Button} icon="refresh" disabled=${busy || status.state === 'disabled'} onClick=${() => desktop.update.check()}>Check for updates<//>
      ${status.state === 'ready' && html`<${Button} kind="primary" icon="check" onClick=${() => desktop.update.install()}>Restart and install ${status.version}<//>`}
    </div>
  <//>`;
}

// ------------------------------------------------------------ diagnostics

const KEY_NAMES = { playpause: 'Play/Pause', next: 'Next', previous: 'Previous', stop: 'Stop' };
const failed = (part) => part && part.error;

/** The report as rows: { ok: true | false | null, label, detail }. null means "just so you know". */
function diagnosticRows(report, keys, update) {
  const rows = [];
  const { database: db, library, drives, devices, services, jobs } = report;
  if (failed(db)) rows.push({ ok: false, label: 'Library database', detail: db.error });
  else {
    rows.push({ ok: true, label: 'Library database', detail: `${fmt.bytes(db.size)} · ${fmt.plural(db.songs, 'song')} · ${fmt.plural(db.plays, 'play')} recorded · ${fmt.plural(db.playlists, 'playlist')}` });
    if (db.missing) rows.push({ ok: false, label: 'Songs that cannot be found', detail: `${fmt.number(db.missing)} (usually an unplugged drive). See Library tools, Missing files.` });
  }
  if (failed(library)) rows.push({ ok: false, label: 'Music folders', detail: library.error });
  else if (!library.length) rows.push({ ok: null, label: 'Music folders', detail: 'None added yet.' });
  else for (const f of library) rows.push({ ok: f.available, label: 'Music folder', detail: f.available ? `${f.path}${f.free != null ? ` · ${fmt.bytes(f.free)} free of ${fmt.bytes(f.total)}` : ''}` : `${f.path} · not found. Is the drive connected?` });
  if (failed(drives)) rows.push({ ok: false, label: 'Drives', detail: drives.error });
  else rows.push({ ok: null, label: 'Drives', detail: drives.length ? drives.map((d) => `${d.mount_path} (${[d.fs, d.removable ? 'removable' : null].filter(Boolean).join(', ')})`).join(' · ') : 'None found.' });
  if (failed(devices)) rows.push({ ok: false, label: 'Devices', detail: devices.error });
  else for (const d of devices) rows.push({ ok: d.connected, label: `Device “${d.label}”`, detail: `${d.connected ? 'connected' : 'not connected'} · ${fmt.plural(d.synced, 'song')} copied${d.last_synced_at ? ` · last synced ${fmt.date(d.last_synced_at)}` : ''}` });
  if (!failed(services)) {
    const lb = services.listenbrainz;
    const sc = lb.scrobbler;
    rows.push({
      ok: !lb.has_token ? null : sc?.state === 'rejected' || sc?.state === 'waiting' ? false : true,
      label: 'ListenBrainz',
      detail: !lb.has_token ? 'No token saved.' : `token saved · recording plays ${lb.scrobble && lb.enabled ? 'on' : 'off'}${sc && sc.active ? ` · ${sc.state}${sc.pending ? `, ${fmt.plural(sc.pending, 'play')} waiting` : ''}${sc.last_error ? `: ${sc.last_error}` : ''}` : ''}`,
    });
    rows.push({ ok: null, label: 'Last.fm and MusicBrainz', detail: `Last.fm key ${services.lastfm.has_key ? 'saved' : 'not set'} · MusicBrainz contact ${services.musicbrainz.has_contact ? 'set' : 'not set'}` });
  }
  if (update) rows.push({ ok: update.state === 'error' ? false : null, label: 'Updates', detail: update.state === 'error' ? update.error : `${update.state}${update.version ? ` (${update.version})` : ''} · this is ${update.current}` });
  if (keys) {
    const lost = Object.entries(keys).filter(([, held]) => !held).map(([key]) => KEY_NAMES[key] || key);
    rows.push({ ok: lost.length === 0, label: 'Keyboard media keys', detail: lost.length === 0 ? 'Play/Pause, Next, Previous and Stop are claimed by this app.' : `Another program is holding: ${lost.join(', ')}. Close other media players or restart the computer.` });
  }
  if (!failed(jobs) && jobs.recent_failures.length) for (const j of jobs.recent_failures) rows.push({ ok: false, label: 'A task failed', detail: `${j.title}: ${j.error}` });
  return rows;
}

function reportText(report, rows) {
  const home = report.paths.home;
  const hide = (text) => (home ? String(text).split(home).join('~') : String(text));
  const lines = [
    `Music Toolkit ${report.app.version} on ${report.app.platform} (Python ${report.app.python}, ${report.app.installed ? 'installed app' : 'from source'})`,
    `Report made ${report.generated_at}`,
    `Data folder: ${report.paths.data_dir}`,
    '',
    ...rows.map((r) => `${r.ok === true ? '[ok]  ' : r.ok === false ? '[!!]  ' : '[..]  '}${r.label}: ${r.detail}`),
  ];
  return hide(lines.join('\n'));
}

function DiagnosticsCard() {
  const { status: update } = useStore(updates);
  const [result, setResult] = useState(null); // { report, keys }
  const [busy, setBusy] = useState(false);

  async function run() {
    setBusy(true);
    try {
      const [report, keys] = await Promise.all([api('/system/diagnostics'), desktop?.mediaKeys ? desktop.mediaKeys() : null]);
      setResult({ report, keys });
    } catch (error) {
      notifyError(error);
    }
    setBusy(false);
  }
  const rows = result ? diagnosticRows(result.report, result.keys, desktop?.update ? update : null) : [];
  return html`<${Card} icon="wrench" title="Diagnostics" hint="Checks the library, folders, drives, accounts and keyboard keys, and writes a report you can copy when something does not work. It never includes passwords, tokens or keys.">
    <div class="row-actions">
      <${Button} icon="search" disabled=${busy} onClick=${run}>${busy ? 'Checking…' : result ? 'Check again' : 'Run diagnostics'}<//>
      ${result && html`<${Button} icon="copy" onClick=${() => navigator.clipboard?.writeText(reportText(result.report, rows)).then(() => toast('Report copied', 'success', 2200), () => toast('Could not copy. Select the text instead.', 'error'))}>Copy report<//>`}
      ${result && desktop?.openPath && html`<${Button} icon="folder" onClick=${() => desktop.openPath(result.report.paths.logs)}>Open the logs folder<//>`}
    </div>
    ${result && html`<ul class="diag-list" aria-label="Diagnostics results">${rows.map((r, i) => html`<li class="diag ${r.ok === true ? 'ok' : r.ok === false ? 'bad' : ''}" key=${i}>
      <${Icon} name=${r.ok === true ? 'check' : r.ok === false ? 'alert' : 'info'} size=${15} /><strong>${r.label}</strong><span>${r.detail}</span></li>`)}</ul>`}
  <//>`;
}

function LogViewer() {
  const [data, setData] = useState(null);
  useEffect(() => { api('/system/logs', { params: { tail: 400 } }).then(setData).catch(notifyError); }, []);
  if (!data) return html`<${Spinner} />`;
  return html`<p class="subtle path">${data.path}</p><pre class="log">${data.lines.join('\n') || 'The log is empty.'}</pre>`;
}

function DataCard({ about }) {
  const { theme } = useStore(ui);
  async function backup() {
    try { const result = await api('/system/backup', { method: 'POST' }); toast(`Backup saved: ${result.path}`, 'success', 9000); } catch (error) { notifyError(error); }
  }
  return html`<${Card} icon="sliders" title="Appearance and data">
    <label class="field narrow"><span>Theme</span>
      <select value=${theme} onChange=${(e) => setTheme(e.target.value)}><option value="dark">Dark</option><option value="light">Light</option></select></label>
    ${about && html`<dl class="info-grid">
      <dt>Version</dt><dd>${about.version}</dd>
      <dt>Data folder</dt><dd class="path">${about.data_dir}${desktop?.openPath ? html` <button class="icon-btn" aria-label="Open data folder" onClick=${() => desktop.openPath(about.data_dir)}><${Icon} name="external" size=${14} /></button>` : ''}</dd>
      <dt>Library database</dt><dd class="path">${about.db_path} (${fmt.bytes(about.db_size)})</dd>
    </dl>`}
    <p class="subtle">Your play history, playlists, ratings and favorites live only in the library database. Your music files are separate.</p>
    <div class="row-actions">
      <${Button} icon="download" onClick=${backup}>Back up the database now<//>
      <${Button} icon="list" onClick=${() => openModal({ title: 'Activity log', width: 820, render: () => html`<${LogViewer} />` })}>View the activity log<//>
    </div>
  <//>`;
}

export function SettingsView() {
  const settings = useStore(library, (s) => s.settings);
  const about = useStore(library, (s) => s.about);
  useEffect(() => { loadSettings().catch(notifyError); loadAbout().catch(() => {}); }, []);
  if (!settings) return html`<${Spinner} />`;
  return html`<${PageHeader} title="Settings" />
    <div class="settings">
      <${LibraryCard} settings=${settings} about=${about} />
      <${AccountsCard} settings=${settings} />
      <${LayoutsCard} settings=${settings} />
      <${UpdatesCard} settings=${settings} />
      <${DiagnosticsCard} />
      <${DataCard} about=${about} />
    </div>`;
}
