// Podcasts: the shows you follow, their new episodes, finding more, and a page for each show with its episodes.
// Episodes play through the app, remember where you stopped, and never enter the music history or statistics.
import { api, fmt, html, useAsync, useCallback, useDebounced, useEffect, useState, useStore } from '../lib.js';
import { Icon } from '../icons.js';
import { Button, Cover, Empty, JobBanner, PageHeader, Spinner, TabPanel, Tabs } from '../components.js';
import { confirmDialog } from '../dialogs.js';
import { isEpisode } from '../items.js';
import { current, player, toggle } from '../player.js';
import { checkForNewEpisodes, playEpisodes, queueEpisode, setHeard } from '../podcasts.js';
import { go, href, notifyError, onJobFinished, openMenu, route, toast } from '../state.js';

const TABS = [
  { id: 'subscriptions', label: 'Subscriptions', icon: 'mic' },
  { id: 'new', label: 'New episodes', icon: 'sparkles' },
  { id: 'find', label: 'Find', icon: 'search' },
];

/** The shows are drawn like other covers: `kind: 'podcast'` is only for the picture and the colour. */
const asShow = (podcast) => ({ ...podcast, kind: 'podcast' });

/** Run `reload` whenever a check for new episodes finishes (that is when there may be something new to show). */
function useCheckFinished(reload) {
  useEffect(() => onJobFinished((job) => { if (job.kind === 'podcasts') reload(); }), [reload]);
}

// ------------------------------------------------------------ an episode

function EpisodeRow({ episode, queue, index, showShow, onChanged }) {
  const [more, setMore] = useState(false);
  const state = useStore(player, (s) => {
    const now = current(s);
    return now && isEpisode(now) && now.id === episode.id ? (s.playing ? 'playing' : 'paused') : '';
  });
  // The episode being played shows how far it has got as it goes; the others show where listening stopped.
  const live = useStore(player, (s) => {
    const now = current(s);
    return now && isEpisode(now) && now.id === episode.id ? Math.floor(s.position) : null;
  });
  const reached = live ?? episode.position;
  const left = episode.duration && reached > 0 && !episode.played ? Math.max(0, episode.duration - reached) : null;
  const meta = [fmt.date(episode.published_at), left !== null ? `${fmt.length(left)} left` : fmt.length(episode.duration), episode.played ? 'Heard' : ''].filter(Boolean).join(' · ');
  const pct = episode.duration && reached > 0 && !episode.played ? Math.min(100, (reached / episode.duration) * 100) : 0;
  const long = (episode.description || '').length > 240;

  const press = () => (state ? toggle() : playEpisodes(queue, index));

  async function mark(played) {
    try {
      await setHeard(episode, played);
      onChanged();
    } catch (error) {
      notifyError(error);
    }
  }

  function openActions(event) {
    event.stopPropagation(); // the click that opens a menu must not reach the window, which closes menus on a click
    const box = event.currentTarget.getBoundingClientRect();
    openMenu({ clientX: box.left, clientY: box.bottom }, [
      { label: episode.played ? 'Mark as not heard' : 'Mark as heard', icon: episode.played ? 'undo' : 'check', action: () => mark(!episode.played) },
      { label: 'Play next', icon: 'next', action: () => queueEpisode(episode, { next: true }) },
      { label: 'Add to queue', icon: 'queue', action: () => queueEpisode(episode) },
      ...(showShow ? [{ divider: true }, { label: 'Go to the podcast', icon: 'mic', action: () => go(`/podcast/${episode.podcast_id}`) }] : []),
    ]);
  }

  return html`<article class="episode ${state ? 'current' : ''} ${episode.played ? 'heard' : ''}">
    <button class="ep-play" aria-label=${`${state === 'playing' ? 'Pause' : 'Play'} ${episode.title}`} onClick=${press}>
      <${Cover} item=${episode} name=${episode.podcast_title || episode.title} size=64 />
      <span class="ep-play-overlay">${state === 'playing' ? html`<span class="eq-bars"><i></i><i></i><i></i></span>` : html`<${Icon} name="play" size=${18} />`}</span>
    </button>
    <div class="ep-main">
      <div class="ep-title">${episode.title}${episode.new && html`<span class="badge">New</span>`}</div>
      ${showShow && html`<a class="ep-show" href=${href(`/podcast/${episode.podcast_id}`)}>${episode.podcast_title}</a>`}
      <div class="ep-meta subtle">${meta}</div>
      ${pct > 0 && html`<div class="progress thin" aria-hidden="true"><div style=${{ width: pct + '%' }}></div></div>`}
      ${episode.description ? html`<p class="ep-desc ${more ? 'open' : ''}">${episode.description}</p>` : null}
      ${long && html`<button type="button" class="link-btn" aria-expanded=${more} onClick=${() => setMore(!more)}>${more ? 'Show less' : 'Show more'}</button>`}
    </div>
    <button class="icon-btn" aria-label=${`More about ${episode.title}`} aria-haspopup="menu" onClick=${openActions}><${Icon} name="more" size=${18} /></button>
  </article>`;
}

function EpisodeList({ episodes, showShow, onChanged, source }) {
  return html`<div class="episodes">${episodes.map((e, i) => html`<${EpisodeRow} key=${e.id} episode=${e} queue=${episodes} index=${i} showShow=${showShow} onChanged=${onChanged} source=${source} />`)}</div>`;
}

// ------------------------------------------------------------ the shows followed

function PodcastCard({ podcast }) {
  return html`<a class="card podcast-card" href=${href(`/podcast/${podcast.id}`)}>
    <div class="card-art">
      <${Cover} item=${asShow(podcast)} name=${podcast.title} size=200 />
      ${podcast.new > 0 && html`<span class="new-flag">${podcast.new} new</span>`}
    </div>
    <div class="card-title">${podcast.title}</div>
    <div class="card-sub">${podcast.author || 'Podcast'}</div>
    <div class="card-sub">${podcast.error ? 'Could not be checked' : podcast.latest ? `Latest ${fmt.date(podcast.latest)}` : 'No episodes yet'}</div>
  </a>`;
}

function Subscriptions({ data, error, setTab }) {
  if (error) return html`<${Empty} icon="alert" title="Your podcasts could not be read">${error.message}<//>`;
  if (!data) return html`<${Spinner} />`;
  if (!data.items.length) {
    return html`<${Empty} icon="mic" title="You don't follow any podcasts yet">
      <p>Find a show by its name, or follow one by the address of its feed. New episodes are looked for in the background and listed under New episodes; where you stop is remembered.</p>
      <${Button} kind="primary" icon="search" onClick=${() => setTab('find')}>Find podcasts<//>
    <//>`;
  }
  return html`<div class="grid">${data.items.map((p) => html`<${PodcastCard} key=${p.id} podcast=${p} />`)}</div>`;
}

// ------------------------------------------------------------ new episodes

function NewEpisodes({ version, onChanged }) {
  const { data, error } = useAsync(() => api('/podcasts/new', { params: { limit: 100 } }), [version]);
  if (error) return html`<${Empty} icon="alert" title="The new episodes could not be read">${error.message}<//>`;
  if (!data) return html`<${Spinner} />`;
  if (!data.items.length) {
    return html`<${Empty} icon="sparkles" title="Nothing new">
      <p>Episodes that come out after you follow a show are listed here until you have heard them, together with the latest few of a show you have just followed and any you have started.</p>
    <//>`;
  }
  return html`
    <div class="row-actions">
      <${Button} kind="primary" icon="play" onClick=${() => playEpisodes(data.items, 0, 'New episodes')}>Play them in turn<//>
      <span class="subtle">${fmt.plural(data.total, 'new episode')}</span>
    </div>
    <${EpisodeList} episodes=${data.items} showShow=${true} onChanged=${onChanged} />`;
}

// ------------------------------------------------------------ finding shows

/** The store the person is likely to use, from the language of the page ("en-GB" gives GB). */
const storeCountry = () => (navigator.language?.split('-')[1] || 'US').toUpperCase();

function ResultCard({ show, onFollowed }) {
  const [busy, setBusy] = useState(false);
  async function follow() {
    setBusy(true);
    try {
      const podcast = await api('/podcasts', { method: 'POST', body: { feed_url: show.feed_url, itunes_id: show.itunes_id || undefined } });
      toast(`Following "${podcast.title}"`, 'success');
      onFollowed(show, podcast);
    } catch (error) {
      notifyError(error);
    } finally {
      setBusy(false);
    }
  }
  const sub = [show.genre, show.episodes ? fmt.plural(show.episodes, 'episode') : ''].filter(Boolean).join(' · ');
  return html`<div class="card result-card">
    <div class="card-art"><${Cover} item=${asShow({ id: show.followed_id || 0, has_art: Boolean(show.has_art) })} name=${show.title} size=200 /></div>
    <div class="card-title" title=${show.title}>${show.title}</div>
    <div class="card-sub">${show.author || 'Podcast'}</div>
    ${sub ? html`<div class="card-sub">${sub}</div>` : null}
    <div class="card-foot">
      ${show.followed_id
        ? html`<a class="btn small" href=${href(`/podcast/${show.followed_id}`)}><${Icon} name="check" size=${15} /><span>Following</span></a>`
        : html`<button type="button" class="btn primary small" aria-label=${`Follow ${show.title}`} disabled=${busy} onClick=${follow}>
            <${Icon} name="plus" size=${15} /><span>${busy ? 'Following…' : 'Follow'}</span></button>`}
    </div>
  </div>`;
}

function FollowByAddress({ onFollowed }) {
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState('');
  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setProblem('');
    try {
      const podcast = await api('/podcasts', { method: 'POST', body: { feed_url: url.trim() } });
      toast(`Following "${podcast.title}"`, 'success');
      setUrl('');
      onFollowed(null, podcast);
    } catch (error) {
      setProblem(error.message);
    } finally {
      setBusy(false);
    }
  }
  return html`<form class="card-panel add-form" onSubmit=${submit}>
    <h2><${Icon} name="link" size=${20} /> Follow a podcast by its feed address</h2>
    <p class="subtle">For a show that is not in the directory: paste the address of its RSS feed. The feed is read first, and an address that is not a podcast is not followed.</p>
    <label class="field"><span>Feed address</span>
      <input type="url" value=${url} required placeholder="https://example.com/feed.xml" onInput=${(e) => setUrl(e.target.value)} /></label>
    ${problem ? html`<p class="note warn" role="alert"><${Icon} name="alert" size=${14} /><span>${problem}</span></p>` : null}
    <div class="row-actions"><${Button} kind="primary" icon="plus" type="submit" disabled=${busy || !url.trim()}>${busy ? 'Reading the feed…' : 'Follow this feed'}<//></div>
  </form>`;
}

function FindPodcasts({ onFollowed }) {
  const [text, setText] = useState('');
  const term = useDebounced(text.trim(), 400);
  const [found, setFound] = useState({ items: [], loading: false, error: null });
  useEffect(() => {
    if (!term) {
      setFound({ items: [], loading: false, error: null });
      return undefined;
    }
    let stale = false;
    setFound((f) => ({ ...f, loading: true, error: null }));
    api('/podcasts/search', { params: { q: term, country: storeCountry() } }).then(
      (answer) => !stale && setFound({ items: answer.items, loading: false, error: null }),
      (error) => !stale && setFound({ items: [], loading: false, error }),
    );
    return () => { stale = true; };
  }, [term]);

  const followed = (show, podcast) => {
    if (show) setFound((f) => ({ ...f, items: f.items.map((s) => (s.feed_url === show.feed_url ? { ...s, followed_id: podcast.id, has_art: podcast.has_art } : s)) }));
    onFollowed(podcast);
  };

  return html`<div class="find">
    <label class="search-box"><${Icon} name="search" size=${16} />
      <input type="search" value=${text} placeholder="Search for a podcast" aria-label="Search for a podcast" onInput=${(e) => setText(e.target.value)} /></label>
    ${found.error
      ? html`<${Empty} icon="alert" title="The directory could not be reached"><p>${found.error.message}</p><//>`
      : found.loading && !found.items.length
        ? html`<${Spinner} label="Searching…" />`
        : term && !found.items.length
          ? html`<${Empty} icon="search" title="No podcasts found"><p>Try other words, or follow the show by its feed address below.</p><//>`
          : found.items.length > 0
            ? html`<div class="grid">${found.items.map((s) => html`<${ResultCard} key=${s.feed_url} show=${s} onFollowed=${followed} />`)}</div>`
            : html`<p class="subtle pad">Search Apple's podcast directory by the name of a show or a subject. Only the words you type are sent.</p>`}
    <${FollowByAddress} onFollowed=${(_, podcast) => onFollowed(podcast)} />
  </div>`;
}

// ------------------------------------------------------------ the view

export function PodcastsView() {
  const { query } = useStore(route);
  const [tab, setTab] = useState(TABS.some((t) => t.id === query.tab) ? query.tab : 'subscriptions');
  useEffect(() => { if (TABS.some((t) => t.id === query.tab)) setTab(query.tab); }, [query.tab]); // a link to #/podcasts?tab=find from where the screen is already open
  const [version, setVersion] = useState(0);
  const reload = useCallback(() => setVersion((v) => v + 1), []);
  useCheckFinished(reload);
  const { data, error } = useAsync(() => api('/podcasts'), [version]);
  const newCount = data ? data.items.reduce((sum, p) => sum + p.new, 0) : undefined;

  return html`
    <${PageHeader} title="Podcasts" subtitle="Shows you follow, played through the app. Where you stop is remembered, and nothing here enters your music history.">
      ${data && data.items.length > 0 && html`<${Button} icon="refresh" disabled=${data.refreshing} onClick=${() => checkForNewEpisodes()}>Check for new episodes<//>`}
    <//>
    <${JobBanner} kinds=${['podcasts']} />
    <${Tabs} id="pod" label="Podcasts" value=${tab} onChange=${setTab}
      tabs=${TABS.map((t) => ({ ...t, count: t.id === 'subscriptions' ? data?.items.length || undefined : t.id === 'new' ? newCount || undefined : undefined }))} />
    <${TabPanel} id="pod" value=${tab}>
      ${tab === 'subscriptions' && html`<${Subscriptions} data=${data} error=${error} setTab=${setTab} />`}
      ${tab === 'new' && html`<${NewEpisodes} version=${version} onChanged=${reload} />`}
      ${tab === 'find' && html`<${FindPodcasts} onFollowed=${() => { reload(); }} />`}
    <//>
    <p class="hint-card subtle"><${Icon} name="info" size=${14} /><span>Shows are found in Apple's free podcast directory, which is asked only when you search. New episodes are looked for about every six hours while the app is open (<a href=${href('/settings')}>Settings</a> can switch that off).</span></p>`;
}

// ------------------------------------------------------------ one show

export function PodcastView({ id }) {
  const [version, setVersion] = useState(0);
  const [unheard, setUnheard] = useState(false);
  const [limit, setLimit] = useState(50);
  const reload = useCallback(() => setVersion((v) => v + 1), []);
  useCheckFinished(reload);
  const { data: podcast, error } = useAsync(
    () => api(`/podcasts/${id}`, { params: { limit, unplayed: unheard || undefined } }),
    [id, version, limit, unheard],
  );

  if (error) {
    return html`<${Empty} icon="mic" title=${error.status === 404 ? "You don't follow that podcast" : 'The podcast could not be read'} level=${1}>
      <p>${error.status === 404 ? 'It may have been unfollowed.' : error.message}</p><a class="btn" href=${href('/podcasts')}>Back to your podcasts</a><//>`;
  }
  if (!podcast) return html`<${Spinner} />`;

  const episodes = podcast.items;
  const play = () => {
    const first = episodes.findIndex((e) => !e.played);
    return playEpisodes(episodes, first < 0 ? 0 : first, podcast.title);
  };
  async function markAllHeard() {
    if (!(await confirmDialog({ title: 'Mark all as heard', message: `Mark every episode of "${podcast.title}" as heard?`, confirm: 'Mark as heard' }))) return;
    try {
      const { marked } = await api(`/podcasts/${id}/played`, { method: 'POST' });
      toast(`Marked ${fmt.plural(marked, 'episode')} as heard`, 'success');
      reload();
    } catch (failure) {
      notifyError(failure);
    }
  }
  async function unfollow() {
    if (!(await confirmDialog({ title: 'Unfollow', message: `Stop following "${podcast.title}"? Its episodes and where you stopped are forgotten.`, confirm: 'Unfollow', danger: true }))) return;
    try {
      await api(`/podcasts/${id}`, { method: 'DELETE' });
      toast(`Unfollowed "${podcast.title}"`, 'success');
      go('/podcasts');
    } catch (failure) {
      notifyError(failure);
    }
  }

  return html`
    <div class="hero">
      <${Cover} item=${asShow(podcast)} name=${podcast.title} size=232 class="hero-art" />
      <div class="hero-text">
        <span class="eyebrow">Podcast</span>
        <h1>${podcast.title}</h1>
        ${podcast.author ? html`<p>${podcast.author}</p>` : null}
        <p class="subtle">${fmt.plural(podcast.episodes, 'episode')}${podcast.unplayed ? ` · ${podcast.unplayed} not heard` : ''}${podcast.last_checked_at ? ` · checked ${fmt.ago(Date.parse(podcast.last_checked_at) / 1000)}` : ''}</p>
        <div class="hero-actions">
          <${Button} kind="primary" icon="play" onClick=${play} disabled=${!episodes.length}>Play<//>
          <${Button} icon="refresh" onClick=${() => checkForNewEpisodes(podcast.id)}>Check now<//>
          <${Button} icon="check" onClick=${markAllHeard} disabled=${!podcast.unplayed}>Mark all as heard<//>
          <${Button} kind="danger-ghost" icon="trash" onClick=${unfollow}>Unfollow<//>
        </div>
      </div>
    </div>
    ${podcast.error ? html`<p class="note warn" role="status"><${Icon} name="alert" size=${14} /><span>The last check for new episodes failed: ${podcast.error}</span></p>` : null}
    ${podcast.description ? html`<details class="about"><summary>About this podcast</summary><p class="plain-notes">${podcast.description}</p>
      ${podcast.site_url && html`<p><a href=${podcast.site_url} target="_blank" rel="noopener noreferrer"><${Icon} name="external" size=${13} /> Website</a></p>`}</details>` : null}
    <div class="section-bar">
      <h2 class="section-title">Episodes</h2>
      <div class="chip-row" role="group" aria-label="Which episodes">
        <button type="button" class="chip ${unheard ? '' : 'on'}" aria-pressed=${!unheard} onClick=${() => { setUnheard(false); setLimit(50); }}>All</button>
        <button type="button" class="chip ${unheard ? 'on' : ''}" aria-pressed=${unheard} onClick=${() => { setUnheard(true); setLimit(50); }}>Not heard</button>
      </div>
    </div>
    ${episodes.length
      ? html`<${EpisodeList} episodes=${episodes} showShow=${false} onChanged=${reload} />
          ${episodes.length < podcast.total && html`<div class="more-row"><${Button} onClick=${() => setLimit(limit + 50)}>Show more episodes<//></div>`}`
      : html`<p class="subtle pad">${unheard ? 'You have heard every episode.' : 'This show has no episodes that can be played.'}</p>`}`;
}
