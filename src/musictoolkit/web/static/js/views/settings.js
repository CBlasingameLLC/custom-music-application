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

function AccountsCard({ settings }) {
  const [mb, setMb] = useState(settings.musicbrainz.contact);
  const [lbUser, setLbUser] = useState(settings.listenbrainz.username);
  const [lbToken, setLbToken] = useState('');
  const [lfKey, setLfKey] = useState('');
  const [lfSecret, setLfSecret] = useState('');
  const lb = settings.listenbrainz;
  const lf = settings.lastfm;
  return html`<${Card} icon="globe" title="Accounts and services" hint="All optional and free. Nothing is sent anywhere until you turn it on.">
    <div class="service">
      <h3>MusicBrainz <span class="badge">tag lookups</span></h3>
      <p class="subtle">MusicBrainz asks apps to identify themselves with a contact address. Used when filling in missing song tags.</p>
      <div class="inline-form"><input type="email" placeholder="you@example.com" value=${mb} onInput=${(e) => setMb(e.target.value)} />
        <${Button} onClick=${() => save({ musicbrainz: { contact: mb } })}>Save<//></div>
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
      <div class="row-actions">
        <${Button} onClick=${async () => { const patch = { username: lbUser }; if (lbToken) patch.user_token = lbToken; if (await save({ listenbrainz: patch })) setLbToken(''); }}>Save ListenBrainz<//>
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
      <${Button} onClick=${async () => { const patch = {}; if (lfKey) patch.api_key = lfKey; if (lfSecret) patch.api_secret = lfSecret; if (Object.keys(patch).length && (await save({ lastfm: patch }))) { setLfKey(''); setLfSecret(''); } }}>Save Last.fm<//>
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
      <${DataCard} about=${about} />
    </div>`;
}
