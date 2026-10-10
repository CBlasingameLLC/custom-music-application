// Stats: what you listen to, when, and how it changed. Everything here comes from the play history, so it grows with it
// (plays made in this app and the Spotify history you imported).
import { api, fmt, html, useAsync, useState, useStore, viewerOffset } from '../lib.js';
import { Icon } from '../icons.js';
import { Button, Empty, PageHeader, Spinner } from '../components.js';
import { ChartCard, ColumnChart, daysBetween, Heatmap, RankedBars } from '../charts.js';
import { playQueue } from '../player.js';
import { enc, go, href, notifyError, route, toast } from '../state.js';

const LAST_YEAR = 'days:365';
const LAST_MONTH = 'days:30';
const SHORT_PERIOD_DAYS = 62; // up to here a period is shown as one column per day; beyond it as a grid of days
const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

const utcDate = (year, month, day = 1, hour = 0) => new Date(Date.UTC(year, month, day, hour));
const inUtc = (date, options) => date.toLocaleDateString(undefined, { ...options, timeZone: 'UTC' });

/** "2025-03" -> "Mar" (or "Mar ’25"), and the long form for a tooltip. */
function monthParts(key) {
  const [year, month] = key.split('-').map(Number);
  const date = utcDate(year, month - 1);
  return { year, month, short: inUtc(date, { month: 'short' }), long: inUtc(date, { month: 'long', year: 'numeric' }) };
}

/** "2025-03-14" in the words of the viewer's locale. The date has no time zone of its own, so it is never shifted. */
function dayText(iso, options = { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' }) {
  const [year, month, day] = iso.split('-').map(Number);
  return inUtc(utcDate(year, month - 1, day), options);
}

const hourText = (hour) => utcDate(2000, 0, 1, hour).toLocaleTimeString(undefined, { hour: 'numeric', timeZone: 'UTC' });

const trim = (x) => String(Math.round(x * 10) / 10).replace(/\.0$/, '');

/** The headline figure: hours once there are some, minutes before that. */
export function listened(minutes) {
  if (minutes < 60) return { value: String(minutes), unit: minutes === 1 ? 'minute' : 'minutes' };
  const hours = minutes / 60;
  return { value: hours >= 100 ? Math.round(hours).toLocaleString() : trim(hours), unit: trim(hours) === '1' ? 'hour' : 'hours' };
}

/** What to open on: the last 12 months when there is anything in them, else the latest year with plays, else everything. */
export function defaultPeriod(years, now = new Date()) {
  if (!years.length) return 'all';
  const latest = Math.max(...years.map((y) => y.year));
  return latest >= now.getFullYear() ? LAST_YEAR : `year:${latest}`;
}

async function playTrack(trackId) {
  try {
    const { items } = await api('/tracks/by-ids', { method: 'POST', body: { ids: [trackId] } });
    if (!items.length) return toast('That song is no longer in your library');
    await playQueue(items, 0, { source: 'Stats' });
  } catch (error) {
    notifyError(error);
  }
}

const search = (term) => href(`/search/${enc(term)}`);

// ------------------------------------------------------------ the page

export function StatsView() {
  const { query } = useStore(route);
  const offset = viewerOffset();
  const { data: years } = useAsync(() => api('/stats/years', { params: { tz: offset } }), []);
  const [chosen, setChosen] = useState(query.range || null);
  const range = chosen || (years ? defaultPeriod(years.items) : null);
  const { data, error, loading } = useAsync(() => (range ? api('/stats/overview', { params: { range, tz: offset, limit: 10 } }) : null), [range]);

  const options = [[LAST_MONTH, 'Last 30 days'], [LAST_YEAR, 'Last 12 months'], ...(years?.items || []).map((y) => [`year:${y.year}`, String(y.year)]), ['all', 'All time']];
  const known = options.some(([value]) => value === range);
  const picker = html`<label class="sort-select"><span>Period</span>
    <select value=${range || ''} onChange=${(e) => setChosen(e.target.value)} disabled=${!years}>
      ${!known && range && html`<option value=${range}>${data?.title || range}</option>`}
      ${options.map(([value, label]) => html`<option value=${value} key=${value}>${label}</option>`)}
    </select></label>`;

  let body;
  if (error) {
    body = html`<${Empty} icon="alert" title="The stats could not be worked out"><p>${error.message}</p><//>`;
  } else if (years && !years.items.length) {
    body = html`<${Empty} icon="chart" title="No listening history yet">
      <p>Songs you play here are logged as you listen, and your Spotify history can be added too. Your stats appear as soon as there is something to count.</p>
      <${Button} kind="primary" icon="upload" onClick=${() => go('/history/import')}>Import Spotify history<//>
    <//>`;
  } else if (!data) {
    body = html`<${Spinner} />`;
  } else if (data.empty) {
    body = html`<${Empty} icon="chart" title=${`No plays in ${data.title.toLowerCase()}`}>
      <p>Pick another period above. Years in your history: ${(years?.items || []).map((y) => y.year).join(', ') || 'none'}.</p>
    <//>`;
  } else {
    body = html`<${Overview} data=${data} range=${range} loading=${loading} />`;
  }

  return html`
    <${PageHeader} title="Stats" subtitle=${data && !data.empty ? data.title : 'What you listen to, and when'}>${picker}<//>
    ${body}`;
}

// ------------------------------------------------------------ one period

function Tile({ label, value, note }) {
  return html`<div class="stat-tile"><dt>${label}</dt><dd>${value}</dd>${note && html`<dd class="note subtle">${note}</dd>`}</div>`;
}

function Overview({ data, range, loading }) {
  const { totals } = data;
  const time = listened(totals.minutes);
  const span = data.end - data.start;
  const firstPlay = totals.first_play ? fmt.date(new Date(totals.first_play * 1000).toISOString()) : '';
  const lastPlay = totals.last_play ? fmt.date(new Date(totals.last_play * 1000).toISOString()) : '';

  return html`<div class="stats ${loading ? 'refreshing' : ''}" aria-busy=${loading}>
    <section class="stats-hero" aria-label="Summary">
      <p class="big-figure"><span class="big">${time.value}</span> <span class="big-unit">${time.unit} of music</span></p>
      <p class="subtle">${fmt.plural(totals.plays, 'play')}${firstPlay && lastPlay ? html`, from ${firstPlay} to ${lastPlay}` : ''}</p>
      <${Highlights} data=${data} />
    </section>

    <dl class="stat-tiles">
      <${Tile} label="Plays" value=${fmt.number(totals.plays)} />
      <${Tile} label="Artists" value=${fmt.number(totals.artists)} />
      <${Tile} label="Songs" value=${fmt.number(totals.tracks)} />
      <${Tile} label="Days with music" value=${fmt.number(totals.active_days)} />
      ${data.streak.days > 1 && html`<${Tile} label="Longest streak" value=${fmt.plural(data.streak.days, 'day')} note=${`${dayText(data.streak.from, { month: 'short', day: 'numeric' })} to ${dayText(data.streak.to, { month: 'short', day: 'numeric', year: 'numeric' })}`} />`}
      ${range !== 'all' && data.discoveries.count > 0 && html`<${Tile} label="New artists" value=${fmt.number(data.discoveries.count)} note="not played before" />`}
    </dl>

    <h2 class="section-title">When you listen</h2>
    <div class="viz-layout"><${WhenCharts} data=${data} span=${span} /></div>
    <h2 class="section-title">What you listen to</h2>
    <div class="viz-layout">
      <${TopLists} data=${data} />
      <${MonthFavorites} items=${data.month_favorites} />
      ${range !== 'all' && data.discoveries.count > 0 && html`<${Discoveries} found=${data.discoveries} />`}
    </div>
  </div>`;
}

/** The answers people look for first, in words: who, what, when. */
function Highlights({ data }) {
  const topArtist = data.top_artists[0];
  const topSong = data.top_tracks[0];
  const busiest = (counts) => (Math.max(...counts) > 0 ? counts.indexOf(Math.max(...counts)) : -1);
  const weekday = busiest(data.by_weekday);
  const hour = busiest(data.by_hour);
  const items = [
    topArtist && ['Most played artist', topArtist.name],
    topSong && ['Most played song', topSong.title],
    weekday >= 0 && ['Busiest day', WEEKDAYS[weekday]],
    hour >= 0 && ['Busiest hour', hourText(hour)],
  ].filter(Boolean);
  if (!items.length) return null;
  return html`<ul class="highlights" aria-label="Highlights">${items.map(([label, value]) => html`<li key=${label}><span class="subtle">${label}</span><strong>${value}</strong></li>`)}</ul>`;
}

// ------------------------------------------------------------ when you listen

function WhenCharts({ data, span }) {
  const days = span / 86400;
  const dayRows = data.by_day;
  const months = data.by_month;
  const yearsSpanned = months.length > 12;
  const everyMonth = months.length <= 24;

  const monthItems = months.map((m) => {
    const parts = monthParts(m.month);
    // Short periods name every month; long ones name the year once, on January, and leave the rest to the tooltip.
    const label = everyMonth ? (parts.month === 1 || m === months[0] ? `${parts.short}${yearsSpanned ? ` ’${String(parts.year).slice(2)}` : ''}` : parts.short) : (parts.month === 1 ? String(parts.year) : '');
    return { label, value: m.plays, heading: parts.long, minutes: m.minutes };
  });
  const hourItems = data.by_hour.map((plays, hour) => ({ label: hourText(hour), value: plays, heading: `${hourText(hour)} to ${hourText((hour + 1) % 24)}` }));
  const weekdayItems = data.by_weekday.map((plays, i) => ({ label: WEEKDAYS[i].slice(0, 3), value: plays, heading: WEEKDAYS[i] }));

  const perDay = dayRows.length && days <= SHORT_PERIOD_DAYS
    ? (() => {
        const counts = new Map(dayRows.map((d) => [d.date, d.plays]));
        return daysBetween(data.start, data.end, data.offset_minutes).map(({ date }, i) => ({
          // the first column and the first of each month say which month they are in, the rest are just the day of the month
          label: i === 0 || date.endsWith('-01') ? dayText(date, { month: 'short', day: 'numeric' }) : dayText(date, { day: 'numeric' }),
          value: counts.get(date) || 0,
          heading: dayText(date),
        }));
      })()
    : null;

  return html`
    ${perDay && html`<${ChartCard} wide title="Plays each day" subtitle=${data.title}
        table=${{ columns: ['Day', 'Plays'], rows: perDay.map((d) => [d.heading, d.value.toLocaleString()]) }}>
      <${ColumnChart} items=${perDay} ariaLabel="Plays on each day" labelWidth=${26} />
    <//>`}
    ${!perDay && dayRows.length > 0 && html`<${ChartCard} wide title="Days you listened" subtitle="Each square is a day; darker means more plays"
        table=${{ columns: ['Day', 'Plays'], rows: dayRows.map((d) => [dayText(d.date), d.plays.toLocaleString()]) }}>
      <${Heatmap} days=${dayRows} startEpoch=${data.start} endEpoch=${data.end} offsetMinutes=${data.offset_minutes} />
    <//>`}
    ${months.length > 2 && html`<${ChartCard} wide title="Plays by month"
        table=${{ columns: ['Month', 'Plays', 'Listening time'], rows: monthItems.map((m, i) => [m.heading, m.value.toLocaleString(), fmt.duration(months[i].minutes * 60)]) }}>
      <${ColumnChart} items=${monthItems} ariaLabel="Plays in each month" thin=${everyMonth} labelWidth=${yearsSpanned ? 54 : 38} />
    <//>`}
    <${ChartCard} title="Time of day" subtitle="When in the day the music is on"
        table=${{ columns: ['Hour', 'Plays'], rows: hourItems.map((h) => [h.heading, h.value.toLocaleString()]) }}>
      <${ColumnChart} items=${hourItems} ariaLabel="Plays by hour of the day" labelWidth=${46} />
    <//>
    <${ChartCard} title="Day of the week"
        table=${{ columns: ['Day', 'Plays'], rows: weekdayItems.map((d) => [d.heading, d.value.toLocaleString()]) }}>
      <${ColumnChart} items=${weekdayItems} ariaLabel="Plays by day of the week" />
    <//>`;
}

// ------------------------------------------------------------ what you listen to

function TopLists({ data }) {
  const artists = data.top_artists.map((a) => ({ key: a.name, label: a.name, value: a.plays, href: search(a.name), sub: a.minutes ? fmt.duration(a.minutes * 60) : '' }));
  const songs = data.top_tracks.map((t, i) => ({
    key: `${i}-${t.title}`, label: t.title, sub: t.artist || '', value: t.plays,
    onClick: t.track_id ? () => playTrack(t.track_id) : undefined, playTitle: `Play ${t.title}`,
  }));
  const albums = data.top_albums.map((a, i) => ({ key: `${i}-${a.album}`, label: a.album, sub: a.artist || '', value: a.plays, href: search(a.album) }));
  const genres = data.top_genres.map((g) => ({ key: g.genre, label: g.genre, value: g.plays }));
  const table = (first, rows) => ({ columns: [first, 'Plays'], rows });

  return html`
    ${artists.length > 0 && html`<${ChartCard} title="Top artists" subtitle="By plays" table=${table('Artist', artists.map((a) => [a.label, a.value.toLocaleString()]))}>
      <${RankedBars} items=${artists} ariaLabel="Top artists by plays" /><//>`}
    ${songs.length > 0 && html`<${ChartCard} title="Top songs" subtitle="Select one to play it" table=${table('Song', songs.map((s) => [`${s.label}${s.sub ? ` by ${s.sub}` : ''}`, s.value.toLocaleString()]))}>
      <${RankedBars} items=${songs} ariaLabel="Top songs by plays" /><//>`}
    ${albums.length > 0 && html`<${ChartCard} title="Top albums" subtitle="By plays" table=${table('Album', albums.map((a) => [`${a.label}${a.sub ? ` by ${a.sub}` : ''}`, a.value.toLocaleString()]))}>
      <${RankedBars} items=${albums} ariaLabel="Top albums by plays" /><//>`}
    ${genres.length > 0 && html`<${ChartCard} title="Top genres" subtitle="From the tags of the songs in your library" table=${table('Genre', genres.map((g) => [g.label, g.value.toLocaleString()]))}>
      <${RankedBars} items=${genres} ariaLabel="Top genres by plays" /><//>`}`;
}

function MonthFavorites({ items }) {
  if (items.length < 3) return null;
  return html`<figure class="viz viz-card wide">
    <figcaption><div><h3>Song of each month</h3><p class="subtle">The one you played most</p></div></figcaption>
    <ol class="month-favorites">${items.map((m) => {
      const month = monthParts(m.month);
      return html`<li key=${m.month}>
        <span class="mf-month">${month.short} <small class="subtle">${month.year}</small></span>
        <span class="mf-song">${m.track_id ? html`<button type="button" class="rk-play" onClick=${() => playTrack(m.track_id)} title=${`Play ${m.title}`}>${m.title}</button>` : html`<span>${m.title}</span>`}
          ${m.artist && html`<small class="subtle">${m.artist}</small>`}</span>
        <span class="mf-plays subtle">${fmt.plural(m.plays, 'play')}</span>
      </li>`;
    })}</ol>
  </figure>`;
}

function Discoveries({ found }) {
  const items = found.items.map((a) => ({
    key: a.name, label: a.name, value: a.plays, href: search(a.name),
    sub: a.first_play ? `first played ${fmt.date(new Date(a.first_play * 1000).toISOString())}` : '',
  }));
  return html`<${ChartCard} wide title="Artists you found" subtitle=${`${fmt.plural(found.count, 'artist')} you had not played before${found.count > items.length ? `, the ${items.length} you played most are shown` : ''}`}
      table=${{ columns: ['Artist', 'First played', 'Plays'], rows: items.map((a) => [a.label, a.sub.replace('first played ', ''), a.value.toLocaleString()]) }}>
    <${RankedBars} items=${items} ariaLabel="New artists by plays" />
  <//>`;
}
