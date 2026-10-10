// Importing a Spotify "Extended streaming history" export: read it, see what is in it, add it, undo it, and send it
// on to ListenBrainz. Nothing is added until the person has seen what the export holds.
import { api, fmt, html, useAsync, useEffect, useRef, useState } from '../lib.js';
import { Icon } from '../icons.js';
import { Button, JobBanner, PageHeader, Spinner } from '../components.js';
import { confirmDialog, pickFile, pickFolder } from '../dialogs.js';
import { go, href, notifyError, onJobFinished, toast, trackJob } from '../state.js';

const KINDS = ['import-preview', 'import-spotify', 'lb-backfill'];

function Summary({ summary }) {
  const max = Math.max(1, ...summary.top_artists.map((a) => a.plays));
  const [from, to] = summary.date_range || [];
  return html`<div class="stat-row">
      <div class="stat"><strong>${fmt.number(summary.music_rows)}</strong><span>${summary.music_rows === 1 ? 'play' : 'plays'} of music</span></div>
      <div class="stat"><strong>${from ? new Date(from).getFullYear() : '–'}${to && new Date(to).getFullYear() !== new Date(from).getFullYear() ? `–${new Date(to).getFullYear()}` : ''}</strong><span>${from ? `${fmt.date(from)} to ${fmt.date(to)}` : 'no dates found'}</span></div>
      <div class="stat"><strong>${fmt.number(summary.podcast_rows_skipped)}</strong><span>podcast rows left out</span></div>
    </div>
    ${summary.top_artists.length > 0 && html`<h3 class="section-title small">Most played in this export</h3>
      <div class="bars">${summary.top_artists.map((a) => html`<div class="bar-row" key=${a.name}><span class="bar-label">${a.name}</span>
        <div class="bar-track"><div class="bar-fill" style=${{ width: (a.plays / max) * 100 + '%' }}></div></div><span class="bar-value">${fmt.number(a.plays)}</span></div>`)}</div>`}`;
}

export function ImportView() {
  const [version, setVersion] = useState(0);
  const { data: earlier } = useAsync(() => api('/history/imports'), [version]);
  const [pending, setPending] = useState(null); // the read-the-export job we are waiting for
  const [preview, setPreview] = useState(null); // { jobId, path, summary }
  const [result, setResult] = useState(null); // what the last import added
  const [problem, setProblem] = useState(null);
  const pendingRef = useRef(null);
  const chosenRef = useRef('');
  pendingRef.current = pending;
  const refresh = () => setVersion((v) => v + 1);

  useEffect(() => onJobFinished((job) => {
    if (job.kind === 'import-preview' && job.id === pendingRef.current) {
      setPending(null);
      if (job.status === 'done') setPreview({ jobId: job.id, path: chosenRef.current, summary: job.result });
      else if (job.status === 'error') setProblem(job.error);
    } else if (job.kind === 'import-spotify') {
      refresh();
      if (job.status === 'done') {
        setResult(job.result);
        setPreview(null);
      } else if (job.status === 'cancelled') {
        toast('Stopped. Nothing was added to your history.', 'info', 8000);
      }
    } else if (job.kind === 'lb-backfill') {
      refresh();
      if (job.status === 'done') {
        const r = job.result || {};
        toast(`Sent ${fmt.plural(r.sent, 'play')} to ListenBrainz.${r.refused ? ` ${fmt.plural(r.refused, 'play')} it refused were skipped.` : ''}`, 'success', 9000);
      } else if (job.status === 'cancelled') {
        toast('Stopped. What was already sent stays sent; press the button again to carry on.', 'info', 9000);
      }
    }
  }), []);

  async function read(path) {
    if (!path) return;
    setProblem(null);
    setPreview(null);
    setResult(null);
    chosenRef.current = path;
    try {
      const { job } = await api('/history/import/preview', { method: 'POST', body: { path } });
      pendingRef.current = job.id; // before the job can finish, not after the next render
      setPending(job.id);
      trackJob(job);
    } catch (error) {
      notifyError(error);
    }
  }
  const chooseZip = async () => read(await pickFile('Choose the ZIP file Spotify sent you', { filters: [{ name: 'Spotify export', extensions: ['zip'] }] }));
  const chooseFolder = async () => read(await pickFolder('Choose the folder you unzipped it into'));

  async function addPlays() {
    try {
      const { job } = await api('/history/import/apply', { method: 'POST', body: { job_id: preview.jobId } });
      trackJob(job);
    } catch (error) {
      notifyError(error);
    }
  }

  async function send() {
    try {
      const { job } = await api('/history/listenbrainz/backfill', { method: 'POST' });
      trackJob(job);
    } catch (error) {
      notifyError(error);
    }
  }

  async function undo(batch) {
    const note = batch.sent
      ? ` ${fmt.number(batch.sent)} of them were already sent to ListenBrainz; removing them here does not remove them there.`
      : '';
    const ok = await confirmDialog({
      title: `Remove ${fmt.plural(batch.plays, 'imported play')}?`,
      message: `They disappear from your listening history and statistics here.${note} Your music files are not touched.`,
      confirm: 'Remove them',
      danger: true,
    });
    if (!ok) return;
    try {
      const removed = await api(`/history/imports/${batch.batch_id}`, { method: 'DELETE' });
      toast(`Removed ${fmt.plural(removed.removed, 'play')}.`, 'success');
      refresh();
    } catch (error) {
      notifyError(error);
    }
  }

  const busy = pending !== null;
  return html`
    <${PageHeader} title="Import Spotify history" subtitle="Bring your Spotify listening history in, so your statistics and recommendations start with years of data instead of none.">
      <${Button} icon="chevron-left" onClick=${() => go('/history')}>Listening history<//>
    <//>
    <div class="settings">
    <section class="card-panel">
      <h2><${Icon} name="download" size=${18} /> 1. Ask Spotify for your data</h2>
      <ol class="steps">
        <li>Open <a href="https://www.spotify.com/account/privacy/" target="_blank" rel="noopener noreferrer">your Spotify privacy page <${Icon} name="external" size=${12} /></a> and sign in.</li>
        <li>Under “Download your data”, tick <strong>Extended streaming history</strong> (not “Account data”) and request it. Confirm by email.</li>
        <li>Spotify sends a link, usually within a few days and at most 30. Download the ZIP file.</li>
      </ol>
      <p class="subtle">Music Toolkit reads only the plays and the names of the songs, artists and albums. Nothing leaves your computer unless you choose to send it to ListenBrainz below.</p>
    </section>

    <section class="card-panel">
      <h2><${Icon} name="folder" size=${18} /> 2. Choose what Spotify sent</h2>
      <div class="row-actions">
        <${Button} kind="primary" icon="upload" disabled=${busy} onClick=${chooseZip}>Choose the ZIP file…<//>
        <${Button} icon="folder" disabled=${busy} onClick=${chooseFolder}>Choose the unzipped folder…<//>
      </div>
      <${JobBanner} kinds=${KINDS} />
      ${problem && html`<div class="callout warn" role="alert"><p><${Icon} name="alert" size=${16} /><span>${problem}</span></p></div>`}
      ${preview && html`<${Summary} summary=${preview.summary} />
        <div class="organize-apply">
          <${Button} kind="primary" icon="check" onClick=${addPlays}>Add ${fmt.plural(preview.summary.music_rows, 'play')} to my history<//>
          <span class="subtle">Plays you already have are skipped, so importing the same export twice changes nothing. You can undo an import afterwards.</span>
        </div>`}
      ${result && html`<div class="callout" role="status"><p><${Icon} name="check" size=${16} /><span>Added ${fmt.plural(result.inserted, 'play')}${result.duplicates_skipped ? ` (${fmt.number(result.duplicates_skipped)} were already in your history)` : ''}. ${fmt.number(result.matched_to_library)} of them match songs you own. <a href=${href('/history')}>See your listening history</a>.</span></p></div>`}
    </section>

    ${earlier && html`<section class="card-panel">
      <h2><${Icon} name="globe" size=${18} /> 3. Send it to ListenBrainz <span class="badge">optional</span></h2>
      <p class="subtle">ListenBrainz is a free, non-profit home for listening history, and its recommendations get much better with years of it. This sends the artist, song and album names and when you played them, to your own account.</p>
      ${!earlier.can_send
        ? html`<p class="note"><${Icon} name="info" size=${14} /><span>Add your ListenBrainz token in <a href=${href('/settings')}>Settings</a> to turn this on.</span></p>`
        : earlier.unsent > 0
          ? html`<div class="row-actions"><${Button} icon="upload" disabled=${busy} onClick=${send}>Send ${fmt.plural(earlier.unsent, 'play')} to ListenBrainz<//><span class="subtle">Safe to stop and start again: what was sent is remembered.</span></div>`
          : html`<p class="note"><${Icon} name="check" size=${14} /><span>${earlier.items.length ? 'Everything you imported has been sent.' : 'Import some history first.'}</span></p>`}
    </section>`}

    ${earlier?.items.length > 0 && html`<section class="card-panel">
      <h2><${Icon} name="clock" size=${18} /> Earlier imports</h2>
      <div class="batch-list">
        ${earlier.items.map((batch) => html`<div class="batch-row" key=${batch.batch_id}>
          <span class="batch-when">${fmt.date(batch.imported_at)}</span>
          <span>${fmt.plural(batch.plays, 'play')} · ${fmt.number(batch.matched)} match your songs · ${batch.sent ? `${fmt.number(batch.sent)} sent to ListenBrainz` : 'not sent'}</span>
          <span class="spacer"></span>
          <${Button} small icon="undo" onClick=${() => undo(batch)}>Undo<//>
        </div>`)}
      </div>
    </section>`}
    ${!earlier && html`<${Spinner} />`}
    </div>`;
}
