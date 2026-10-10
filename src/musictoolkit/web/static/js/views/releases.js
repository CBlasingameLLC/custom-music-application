// New releases by artists you play (the release radar). Links only: nothing here downloads music.
import { api, fmt, html, useAsync, useState, useStore } from '../lib.js';
import { Icon } from '../icons.js';
import { Button, Cover, Empty, Spinner } from '../components.js';
import { go, href, jobs, library, notifyError, trackJob } from '../state.js';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const DAY = 86400000;

/** "Released 3 days ago", "Out in 12 days", "Out Oct 24"; a date with only a month is named by the month. */
export function whenText(date, now = new Date()) {
  const [year, month, day] = String(date).split('-').map(Number);
  if (!day) return `${MONTHS[month - 1]} ${year}`;
  const days = Math.round((Date.UTC(year, month - 1, day) - Date.UTC(now.getFullYear(), now.getMonth(), now.getDate())) / DAY);
  const named = `${MONTHS[month - 1]} ${day}${year === now.getFullYear() ? '' : `, ${year}`}`;
  if (days === 0) return 'Out today';
  if (days === 1) return 'Out tomorrow';
  if (days > 1) return days <= 21 ? `Out in ${days} days (${named})` : `Out ${named}`;
  if (days === -1) return 'Released yesterday';
  return days >= -21 ? `Released ${-days} days ago` : `Released ${named}`;
}

function Art({ item }) {
  const [failed, setFailed] = useState(false);
  if (!item.art || failed) return html`<${Cover} name=${item.title} size=88 />`;
  return html`<div class="cover"><img src=${item.art} alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" draggable="false" onError=${() => setFailed(true)} /></div>`;
}

function ReleaseCard({ item, dismissed, onStatus }) {
  const played = item.plays > 0 ? `You have played ${item.artist} ${fmt.plural(item.plays, 'time')}` : '';
  return html`<article class="reco release">
    <div class="release-art"><${Art} item=${item} /></div>
    <div class="reco-main">
      <h3>${item.artist} <span class="dash">-</span> ${item.title}</h3>
      <p class="subtle">
        <span class="badge kind">${item.kind}</span>${item.upcoming ? html` <span class="badge soon">Coming soon</span>` : ''}
        ${whenText(item.date)}${played ? ` · ${played}` : ''}
      </p>
      <div class="reco-links">${item.links.map((l) => html`<a class="pill" href=${l.url} target="_blank" rel="noopener noreferrer"><${Icon} name="external" size=${12} /> ${l.label}</a>`)}</div>
    </div>
    <div class="reco-actions">
      ${dismissed
        ? html`<${Button} small icon="refresh" onClick=${() => onStatus(item, 'new')}>Move back<//>`
        : html`<${Button} small icon="x" onClick=${() => onStatus(item, 'dismissed')} title="Hide this release">Dismiss<//>`}
    </div>
  </article>`;
}

/** The tab on Discover. `onCounts` tells the page how many are new, for the badge on the tab. */
export function ReleasesTab({ onCounts }) {
  const rev = useStore(library, (s) => s.rev);
  const active = useStore(jobs, (s) => s.items.some((j) => j.kind === 'radar' && (j.status === 'queued' || j.status === 'running')));
  const [showDismissed, setShowDismissed] = useState(false);
  const [version, setVersion] = useState(0);
  const { data, error } = useAsync(
    () => api('/releases', { params: { status: showDismissed ? 'dismissed' : 'new' } }).then((d) => { onCounts?.(d.counts.new); return d; }),
    [rev, version, showDismissed],
  );

  async function look() {
    try {
      const { job } = await api('/releases/refresh', { method: 'POST' });
      trackJob(job);
    } catch (e) { notifyError(e); }
  }
  async function setStatus(item, status) {
    try { await api(`/releases/${item.id}/status`, { method: 'POST', body: { status } }); setVersion((v) => v + 1); } catch (e) { notifyError(e); }
  }

  if (error) return html`<${Empty} icon="alert" title="Could not load new releases">${error.message}<//>`;
  if (!data) return html`<${Spinner} />`;
  const nowhere = !data.sources.listenbrainz && !data.sources.musicbrainz;
  const checked = data.state.last_run ? `Last checked ${fmt.ago(new Date(data.state.last_run).getTime() / 1000)}${data.state.source ? ` (${data.state.source === 'listenbrainz' ? 'ListenBrainz' : 'MusicBrainz'})` : ''}` : 'Not checked yet';

  const header = html`<div class="radar-bar">
    <span class="subtle">${checked}${data.enabled ? ' · checks by itself about once a day' : ''}</span>
    <span class="spacer"></span>
    <${Button} kind="primary" icon="refresh" disabled=${active || nowhere} onClick=${look}>${active ? 'Looking…' : 'Check for new releases'}<//>
  </div>`;

  if (nowhere && data.items.length === 0) {
    return html`${header}<${Empty} icon="compass" title="New releases from artists you play">
      <p>The radar finds albums, EPs and singles released in the last two months, or announced for the next six weeks, by artists you play. It needs somewhere to look: your <strong>ListenBrainz username</strong>, or a <strong>MusicBrainz contact</strong> (an email address or a web address, which MusicBrainz asks every app to give).</p>
      <${Button} kind="primary" icon="sliders" onClick=${() => go('/settings')}>Open Settings<//>
    <//>`;
  }
  const empty = showDismissed
    ? html`<${Empty} icon="check" title="Nothing dismissed"><p>Releases you dismiss are kept here, so they do not come back.</p><//>`
    : html`<${Empty} icon="compass" title=${data.state.last_run ? 'No new releases right now' : 'Nothing checked yet'}>
        <p>${data.state.last_run
          ? 'None of the artists you play have a release in the last two months, or announced for the next six weeks, that is not already in your library.'
          : 'Press Check for new releases. It looks at the artists you play most; nothing is downloaded.'}</p>
        ${data.state.message && html`<p class="subtle">${data.state.message}</p>`}
      <//>`;

  return html`${header}
    ${data.items.length === 0 ? empty : html`<div class="reco-list">${data.items.map((item) => html`<${ReleaseCard} key=${item.id} item=${item} dismissed=${showDismissed} onStatus=${setStatus} />`)}</div>`}
    <p class="radar-foot subtle">
      <button type="button" class="link-btn" onClick=${() => setShowDismissed((v) => !v)}>${showDismissed ? 'Back to new releases' : `Dismissed${data.counts.dismissed ? ` (${data.counts.dismissed})` : ''}`}</button>
      ${' · '}<a href=${href('/settings')}>${data.enabled ? 'Automatic checks are on' : 'Turn on automatic checks'}</a>
    </p>`;
}
