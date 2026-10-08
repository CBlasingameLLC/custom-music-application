// Dialogs: prompts, confirmations, file/folder pickers, add-to-playlist, track details.
import { api, fmt, html, useEffect, useRef, useState, useStore } from './lib.js';
import { Icon } from './icons.js';
import { Button, Cover, Rating, Spinner } from './components.js';
import { bumpLibrary, desktop, go, href, library, loadPlaylists, notifyError, openModal, toast, trackJob } from './state.js';
import { openTagEditor } from './tagtools.js';

function PromptBody({ label, value, placeholder, confirm, help, onSubmit }) {
  const [text, setText] = useState(value);
  const input = useRef();
  useEffect(() => input.current?.focus(), []);
  return html`<form onSubmit=${(e) => { e.preventDefault(); if (text.trim()) onSubmit(text.trim()); }}>
    <label class="field">
      <span>${label}</span>
      <input ref=${input} type="text" value=${text} placeholder=${placeholder} onInput=${(e) => setText(e.target.value)} />
    </label>
    ${help && html`<p class="subtle">${help}</p>`}
    <div class="modal-actions"><${Button} kind="primary" type="submit" disabled=${!text.trim()}>${confirm}<//></div>
  </form>`;
}

/** Ask for a line of text. Resolves to the trimmed text, or null if dismissed. */
export function promptText({ title, label, value = '', placeholder = '', confirm = 'OK', help }) {
  return new Promise((resolve) => {
    let answer = null;
    openModal({
      title,
      width: 460,
      onClose: () => resolve(answer),
      render: (close) => html`<${PromptBody} label=${label} value=${value} placeholder=${placeholder} confirm=${confirm} help=${help}
        onSubmit=${(text) => { answer = text; close(); }} />`,
    });
  });
}

export function confirmDialog({ title, message, confirm = 'OK', danger = false }) {
  return new Promise((resolve) => {
    let answer = false;
    openModal({
      title,
      width: 460,
      onClose: () => resolve(answer),
      render: (close) => html`<p class="modal-text">${message}</p>
        <div class="modal-actions">
          <${Button} onClick=${close}>Cancel<//>
          <${Button} kind=${danger ? 'danger' : 'primary'} onClick=${() => { answer = true; close(); }}>${confirm}<//>
        </div>`,
    });
  });
}

/** Native folder picker in the desktop app; a typed path in a plain browser. */
export async function pickFolder(title = 'Choose a folder') {
  if (desktop?.selectFolder) return desktop.selectFolder();
  return promptText({ title, label: 'Folder path', placeholder: 'C:\\Users\\you\\Music', confirm: 'Choose' });
}

export async function pickFile(title = 'Choose a file', { filters } = {}) {
  if (desktop?.selectFile) return desktop.selectFile({ filters });
  return promptText({ title, label: 'File path', placeholder: 'C:\\Users\\you\\Downloads\\file', confirm: 'Choose' });
}

// ------------------------------------------------------------ playlists

function AddToPlaylistBody({ ids, close }) {
  const { playlists } = useStore(library);
  const [filter, setFilter] = useState('');
  const [name, setName] = useState('');
  useEffect(() => { loadPlaylists().catch(notifyError); }, []);
  const manual = playlists.filter((p) => p.kind === 'manual' && p.name.toLowerCase().includes(filter.toLowerCase()));

  async function add(playlist) {
    try {
      const result = await api(`/playlists/${playlist.id}/tracks`, { method: 'POST', body: { ids } });
      toast(`Added ${fmt.plural(result.added, 'song')} to "${playlist.name}"`, 'success');
      loadPlaylists();
      bumpLibrary();
      close();
    } catch (error) { notifyError(error); }
  }

  async function create(event) {
    event.preventDefault();
    if (!name.trim()) return;
    try {
      const playlist = await api('/playlists', { method: 'POST', body: { name: name.trim(), track_ids: ids } });
      toast(`Created "${playlist.name}" with ${fmt.plural(ids.length, 'song')}`, 'success');
      loadPlaylists();
      bumpLibrary();
      close();
    } catch (error) { notifyError(error); }
  }

  return html`
    <form class="inline-form" onSubmit=${create}>
      <input type="text" placeholder="New playlist name" value=${name} onInput=${(e) => setName(e.target.value)} />
      <${Button} kind="primary" type="submit" icon="plus" disabled=${!name.trim()}>Create<//>
    </form>
    ${playlists.some((p) => p.kind === 'manual') &&
    html`<input class="search-in-modal" type="search" placeholder="Filter playlists" value=${filter} onInput=${(e) => setFilter(e.target.value)} />
      <div class="pick-list">
        ${manual.map((p) => html`<button class="pick-row" key=${p.id} onClick=${() => add(p)}>
          <${Icon} name="list" size=${16} /><span>${p.name}</span><span class="subtle">${fmt.plural(p.tracks, 'song')}</span>
        </button>`)}
        ${!manual.length && html`<p class="subtle">No playlists match.</p>`}
      </div>`}`;
}

export function openAddToPlaylist(tracks) {
  const ids = tracks.map((t) => t.id);
  openModal({
    title: ids.length === 1 ? `Add "${tracks[0].title}" to a playlist` : `Add ${ids.length} songs to a playlist`,
    width: 460,
    render: (close) => html`<${AddToPlaylistBody} ids=${ids} close=${close} />`,
  });
}

// ------------------------------------------------------------ track details

function TrackInfoBody({ track, close }) {
  const [detail, setDetail] = useState(null);
  useEffect(() => {
    Promise.all([api(`/tracks/${track.id}`), api(`/tracks/${track.id}/info`).catch(() => ({}))])
      .then(([base, info]) => setDetail({ ...base, ...info }))
      .catch(notifyError);
  }, [track.id]);
  if (!detail) return html`<${Spinner} />`;
  const rows = [
    ['Title', detail.title], ['Artist', detail.artist], ['Album artist', detail.album_artist], ['Album', detail.album],
    ['Track', detail.track && (detail.disc ? `${detail.disc}-${detail.track}` : detail.track)], ['Year', detail.year], ['Genre', detail.genre],
    ['Length', fmt.time(detail.duration)], ['Format', [detail.format?.toUpperCase(), fmt.kbps(detail.bitrate || detail.bitrate), detail.sample_rate && `${detail.sample_rate} Hz`].filter(Boolean).join(' · ')],
    ['File size', detail.size && fmt.bytes(detail.size)], ['ReplayGain', typeof detail.replaygain_track_db === 'number' ? `${detail.replaygain_track_db} dB` : ''],
    ['Plays', detail.plays], ['Last played', detail.last_played && fmt.ago(detail.last_played)], ['Added', fmt.date(detail.added)],
    ['MusicBrainz ID', detail.musicbrainz_recording_id],
  ].filter(([, v]) => v !== undefined && v !== null && v !== '');
  return html`
    <div class="info-head">
      <${Cover} trackId=${detail.id} name=${detail.album} size=96 />
      <div><h3>${detail.title}</h3><p class="subtle">${detail.artist} - ${detail.album}</p></div>
    </div>
    <dl class="info-grid">${rows.map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}</dl>
    <dl class="info-grid"><dt>File</dt><dd class="path">${detail.path}
      <button class="icon-btn" title="Copy path" aria-label="Copy path" onClick=${() => navigator.clipboard?.writeText(detail.path).then(() => toast('Path copied', 'success', 1800))}><${Icon} name="copy" size=${14} /></button>
      ${desktop?.showItemInFolder && html`<button class="icon-btn" title="Show in folder" aria-label="Show in folder" onClick=${() => desktop.showItemInFolder(detail.path)}><${Icon} name="folder" size=${14} /></button>`}
    </dd></dl>
    <div class="modal-actions"><${Button} icon="edit" onClick=${() => { close(); openTagEditor([detail.id]); }}>Edit tags<//></div>`;
}

export function openTrackInfo(track) {
  openModal({ title: 'Song details', width: 560, render: (close) => html`<${TrackInfoBody} track=${track} close=${close} />` });
}

/** The "Add music folder" button: pick a folder, save it, and start scanning it. */
export async function addMusicFolder() {
  const path = await pickFolder('Choose the folder your music lives in');
  if (!path) return;
  try {
    const result = await api('/library/roots', { method: 'POST', body: { path } });
    trackJob(result.job);
    toast('Scanning your music. Songs appear as soon as the scan finishes.');
  } catch (error) {
    notifyError(error);
  }
}

export { go, href };
