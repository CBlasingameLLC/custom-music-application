// Discover: new-music suggestions, devices, and listening history.
import { api, fmt, html, useAsync, useCallback, useEffect, useState, useStore } from '../lib.js';
import { Icon } from '../icons.js';
import { Button, Empty, PageHeader, Spinner } from '../components.js';
import { go, href, library, notifyError, toast, trackJob } from '../state.js';

const TABS = [
  ['new', 'New for you'],
  ['accepted', 'Wishlist'],
  ['owned', 'Already own'],
  ['dismissed', 'Not interested'],
];

export function DiscoverView() {
  const rev = useStore(library, (s) => s.rev);
  const [tab, setTab] = useState('new');
  const [version, setVersion] = useState(0);
  const { data, loading } = useAsync(() => api('/recommendations', { params: { status: tab, limit: 200 } }), [tab, rev, version]);

  async function fetchMore() {
    try {
      const { job } = await api('/recommendations/refresh', { method: 'POST', body: { source: 'both', limit: 25 } });
      trackJob(job);
      toast('Fetching recommendations in the background…');
    } catch (error) { notifyError(error); }
  }
  async function triage(item, status) {
    try { await api(`/recommendations/${item.id}/status`, { method: 'POST', body: { status } }); setVersion((v) => v + 1); } catch (error) { notifyError(error); }
  }

  const counts = data?.counts || {};
  return html`
    <${PageHeader} title="Discover" subtitle="New music picked from your listening, minus everything you already own.">
      <${Button} kind="primary" icon="compass" onClick=${fetchMore}>Get recommendations<//>
    <//>
    <div class="tabs" role="tablist">${TABS.map(([id, label]) => html`<button role="tab" class="tab ${tab === id ? 'active' : ''}" aria-selected=${tab === id} onClick=${() => setTab(id)}>
      ${label}${counts[id] ? html`<span class="count">${counts[id]}</span>` : ''}</button>`)}</div>
    ${loading && !data ? html`<${Spinner} />` : data.items.length === 0
      ? html`<${Empty} icon="compass" title=${tab === 'new' ? 'No suggestions yet' : 'Nothing here yet'}>
          <p>${tab === 'new' ? html`Press <strong>Get recommendations</strong>. It needs your ListenBrainz username (an imported listening history makes it far better). Add it in <a href=${href('/settings')}>Settings</a>.` : 'Suggestions you sort will show up here.'}</p>
        <//>`
      : html`<div class="reco-list">${data.items.map((item) => html`<article class="reco" key=${item.id}>
          <div class="reco-main">
            <h3>${item.artist}${item.track ? html` <span class="dash">-</span> ${item.track}` : ''}</h3>
            <p class="subtle">${item.reason} · ${item.source.replace('_', ' ')}${item.suggested ? ` · suggested ${fmt.date(item.suggested)}` : ''}</p>
            <div class="reco-links">${item.links.map((l) => html`<a class="pill" href=${l.url} target="_blank" rel="noopener noreferrer"><${Icon} name="external" size=${12} /> ${l.label}</a>`)}</div>
          </div>
          <div class="reco-actions">
            ${tab === 'new' ? html`
              <${Button} small icon="heart" onClick=${() => triage(item, 'accepted')} title="Add to your wishlist">Want it<//>
              <${Button} small icon="check" onClick=${() => triage(item, 'owned')} title="I already own this">Own it<//>
              <${Button} small icon="x" onClick=${() => triage(item, 'dismissed')} title="Not interested">Skip<//>`
            : html`<${Button} small icon="refresh" onClick=${() => triage(item, 'new')}>Move back to New<//>`}
          </div>
        </article>`)}</div>`}
    <p class="hint-card subtle"><${Icon} name="info" size=${14} /> Music Toolkit never downloads music. “Want it” keeps a wishlist; the links open Bandcamp, YouTube or MusicBrainz in your browser so you can listen first and buy what you love.</p>`;
}

// ------------------------------------------------------------ devices

export function DevicesView() {
  const { data: saved } = useAsync(() => api('/devices'), []);
  const { data: volumes, loading } = useAsync(() => api('/devices/volumes'), []);
  return html`
    <${PageHeader} title="Devices" subtitle="Music players and memory cards you copy music to." />
    <h2 class="section-title">Connected drives</h2>
    ${loading && !volumes ? html`<${Spinner} />` : html`<div class="drive-list">${(volumes?.items || []).map((v) => html`<div class="drive ${v.removable ? 'removable' : ''}">
      <${Icon} name="drive" size=${22} />
      <div><strong>${v.mount_path}</strong><div class="subtle">${v.fs} · ${fmt.bytes(v.free)} free of ${fmt.bytes(v.total)}</div></div>
      ${v.removable && html`<span class="badge">removable</span>`}</div>`)}</div>`}
    <h2 class="section-title">Known devices</h2>
    ${!saved ? html`<${Spinner} />` : saved.items.length === 0
      ? html`<${Empty} icon="drive" title="No devices yet">Drives you copy music to are remembered here.<//>`
      : html`<div class="drive-list">${saved.items.map((d) => html`<div class="drive" key=${d.id}><${Icon} name="drive" size=${22} />
          <div><strong>${d.label || d.mount_path}</strong><div class="subtle">${d.mount_path} · ${fmt.plural(d.synced, 'song')} copied</div></div></div>`)}</div>`}`;
}

// ------------------------------------------------------------ history

export function HistoryView() {
  const [days, setDays] = useState(0);
  const { data: top } = useAsync(() => api('/history/top-artists', { params: { limit: 15, days: days || undefined } }), [days]);
  const { data: recent } = useAsync(() => api('/history/recent', { params: { limit: 40 } }), []);
  const max = Math.max(1, ...(top?.items || []).map((a) => a.plays));
  return html`
    <${PageHeader} title="Listening history" subtitle=${top ? `${fmt.plural(top.total_plays, 'play')} recorded` : ''}>
      <label class="sort-select"><span>Period</span><select value=${days} onChange=${(e) => setDays(Number(e.target.value))}>
        <option value="0">All time</option><option value="365">Last year</option><option value="30">Last 30 days</option><option value="7">Last 7 days</option></select></label>
    <//>
    <h2 class="section-title">Top artists</h2>
    ${!top ? html`<${Spinner} />` : top.items.length === 0
      ? html`<${Empty} icon="chart" title="No plays recorded yet">Songs you play here are logged automatically. Your Spotify history can be imported too.<//>`
      : html`<div class="bars">${top.items.map((a) => html`<div class="bar-row"><span class="bar-label">${a.name}</span>
          <div class="bar-track"><div class="bar-fill" style=${{ width: (a.plays / max) * 100 + '%' }}></div></div><span class="bar-value">${fmt.number(a.plays)}</span></div>`)}</div>`}
    ${recent?.items.length > 0 && html`<h2 class="section-title">Recent plays</h2>
      <div class="recent-list">${recent.items.map((r) => html`<div class="recent-row" key=${r.id}>
        <span class="t1">${r.title || 'Unknown song'}</span><span class="t2">${r.artist || ''}</span>
        <span class="subtle">${fmt.ago(r.played_at)}</span></div>`)}</div>`}`;
}
