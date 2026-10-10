// Charts: hand-built SVG and HTML marks, no chart library.
//
// The rules they follow (the project's data-visualisation method):
// - one question per chart, and the form that answers it: columns for "how much, when", a grid for "which days",
//   ranked bars for "which are biggest"; a single number is a figure, not a chart;
// - one colour for one series (colour follows the entity, never the rank); a magnitude ramp for the grid, which
//   runs light to dark on a light page and dark to light on a dark one;
// - thin marks: columns at most 24px wide, a 4px rounded data end, square on the baseline, 2px of air between
//   neighbours; hairline solid grid; no number on every mark, the tip of the tooltip carries it;
// - text never wears the data colour, labels stay in text tokens;
// - every mark answers hover and keyboard focus with the same tooltip, and every chart has a Table twin, so a value
//   is never reachable only through the pointer.
// Names and values from the server are put on the page as text nodes only.
import { html, useEffect, useRef, useState } from './lib.js';

const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
/** "1 play", "2 plays": the unit is given in the plural. */
const TIP_HEIGHT = 48; // room a tooltip needs above its mark, so it never leaves the chart's own box
const counted = (value, unit) => `${value.toLocaleString()} ${value === 1 ? unit.replace(/s$/, '') : unit}`;

/** 1,200 -> "1.2K", 3,400,000 -> "3.4M"; small numbers stay as they are. */
export function compact(value) {
  const n = Math.abs(value);
  if (n >= 1e6) return `${trim(value / 1e6)}M`;
  if (n >= 1e3) return `${trim(value / 1e3)}K`;
  return String(Math.round(value));
}
const trim = (x) => String(Math.round(x * 10) / 10).replace(/\.0$/, '');

/** The next "round" number at or above a value (1, 2, 5 times a power of ten), so an axis ends on a clean tick. */
export function niceMax(value) {
  if (!(value > 0)) return 1;
  const base = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 5, 10]) if (value <= step * base) return Math.max(1, Math.ceil(step * base));
  return Math.ceil(10 * base);
}

export const ticksFor = (max) => [...new Set([0, Math.round(max / 2), max])];

export function useWidth(ref, fallback = 640) {
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    const element = ref.current;
    if (!element) return undefined;
    const measure = () => setWidth(Math.max(240, Math.floor(element.clientWidth) || fallback));
    measure();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return width;
}

// ------------------------------------------------------------ the card every chart sits in

/** A titled card with the chart and its table twin. `table` is { columns: [..], rows: [[..], ..] }. */
export function ChartCard({ title, subtitle, table, wide = false, children }) {
  const [asTable, setAsTable] = useState(false);
  return html`<figure class="viz viz-card ${wide ? 'wide' : ''}">
    <figcaption>
      <div><h3>${title}</h3>${subtitle && html`<p class="subtle">${subtitle}</p>`}</div>
      ${table && html`<button type="button" class="chip-btn" aria-pressed=${asTable} title=${asTable ? 'Show the chart again' : 'Show these numbers as a table'} onClick=${() => setAsTable(!asTable)}>Table</button>`}
    </figcaption>
    ${asTable && table
      ? html`<div class="viz-table-wrap"><table class="viz-table">
          <caption class="sr-only">${title}</caption>
          <thead><tr>${table.columns.map((c) => html`<th scope="col" key=${c}>${c}</th>`)}</tr></thead>
          <tbody>${table.rows.map((row, i) => html`<tr key=${i}>${row.map((cell, j) => html`<td key=${j}>${cell}</td>`)}</tr>`)}</tbody>
        </table></div>`
      : children}
  </figure>`;
}

// ------------------------------------------------------------ columns

/** items: [{ label, value, heading }]. The tooltip leads with the value; `heading` names what it is the value of. */
/** `thin` lets the chart skip labels that would overlap; pass thin=false with `label: ''` on the columns that get none
 *  when the caller spaces them itself (a year label on each January). `labelWidth` is how much room one label needs. */
export function ColumnChart({ items, unit = 'plays', height = 190, ariaLabel, thin = true, labelWidth = 38 }) {
  const box = useRef();
  const width = useWidth(box);
  const [active, setActive] = useState(null);
  const n = items.length;
  if (!n) return null;
  const left = 40;
  const right = 8;
  const top = 12;
  const bottom = 26;
  const plotW = width - left - right;
  const plotH = height - top - bottom;
  const max = niceMax(Math.max(0, ...items.map((i) => i.value)));
  const band = plotW / n;
  const barW = clamp(band - 2, 2, 24);
  const y = (value) => top + plotH - (value / max) * plotH;
  const cx = (i) => left + band * (i + 0.5);
  const every = thin ? Math.max(1, Math.ceil(labelWidth / band)) : 1;

  const indexAt = (event) => {
    const rect = box.current.getBoundingClientRect();
    const x = event.clientX - rect.left - left;
    return x < 0 || x > plotW ? null : clamp(Math.floor(x / band), 0, n - 1);
  };
  const onKey = (event) => {
    const keys = { ArrowRight: 1, ArrowLeft: -1 };
    if (event.key in keys) setActive(clamp((active ?? (keys[event.key] > 0 ? -1 : n)) + keys[event.key], 0, n - 1));
    else if (event.key === 'Home') setActive(0);
    else if (event.key === 'End') setActive(n - 1);
    else if (event.key === 'Escape') setActive(null);
    else return;
    event.preventDefault();
  };
  const shown = active !== null ? items[active] : null;
  const tipLeft = active !== null ? clamp(cx(active), 64, width - 64) : 0;

  return html`<div class="viz-plot" ref=${box} tabIndex="0" role="group" aria-roledescription="chart" aria-label=${ariaLabel}
      onPointerMove=${(e) => setActive(indexAt(e))} onPointerLeave=${() => setActive(null)} onKeyDown=${onKey} onBlur=${() => setActive(null)}>
    <svg width=${width} height=${height} viewBox="0 0 ${width} ${height}" aria-hidden="true">
      <g class="viz-grid">
        ${ticksFor(max).map((t) => html`<line key=${t} x1=${left} x2=${width - right} y1=${y(t)} y2=${y(t)} class=${t === 0 ? 'axis' : ''} />
          <text key=${'l' + t} x=${left - 8} y=${y(t) + 4} text-anchor="end">${compact(t)}</text>`)}
      </g>
      ${active !== null && html`<rect class="viz-band" x=${left + band * active} y=${top} width=${band} height=${plotH} />`}
      <g class="viz-bars">
        ${items.map((item, i) => item.value > 0 && html`<path key=${i} class="bar ${active === i ? 'hot' : ''}" d=${barPath(cx(i) - barW / 2, y(item.value), barW, top + plotH - y(item.value))} />`)}
      </g>
      <g class="viz-axis-labels">
        ${items.map((item, i) => item.label && i % every === 0 && html`<text key=${i} x=${cx(i)} y=${height - 8} text-anchor="middle">${item.label}</text>`)}
      </g>
    </svg>
    ${shown && html`<div class="viz-tip" style=${{ left: tipLeft + 'px', top: Math.max(TIP_HEIGHT, y(shown.value) - 10) + 'px' }}>
      <strong>${counted(shown.value, unit)}</strong><span>${shown.heading || shown.label}</span>
    </div>`}
    <span class="sr-only" role="status" aria-live="polite">${shown ? `${shown.heading || shown.label}: ${counted(shown.value, unit)}` : ''}</span>
  </div>`;
}

/** A column with a 4px rounded data end (the top) and a square foot on the baseline. */
export function barPath(x, y, w, h) {
  if (h <= 0) return '';
  const r = Math.min(4, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

// ------------------------------------------------------------ the year as a grid of days

const DAY_MS = 86400000;
const WEEKDAYS = ['Mon', '', 'Wed', '', 'Fri', '', ''];

/** Every calendar day from the start of a period to its end, or to today if that comes first, in the viewer's time zone:
 *  [{ t, date }] where `t` is that day's midnight as a UTC timestamp and `date` is 'YYYY-MM-DD'. */
export function daysBetween(startEpoch, endEpoch, offsetMinutes = 0, nowEpoch = Date.now() / 1000) {
  const day = (epoch) => {
    const d = new Date((epoch + offsetMinutes * 60) * 1000);
    return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
  };
  const first = day(startEpoch);
  const last = Math.min(day(endEpoch - 1), day(nowEpoch));
  const days = [];
  for (let t = first; t <= last; t += DAY_MS) days.push({ t, date: new Date(t).toISOString().slice(0, 10) });
  return days;
}

/** days: [{ date: 'YYYY-MM-DD', plays }] inside [startEpoch, endEpoch); dates are in the viewer's time zone (offsetMinutes). */
export function Heatmap({ days, startEpoch, endEpoch, offsetMinutes = 0, unit = 'plays' }) {
  const box = useRef();
  const width = useWidth(box, 720);
  const [active, setActive] = useState(null);
  const counts = new Map(days.map((d) => [d.date, d.plays]));
  const cells = daysBetween(startEpoch, endEpoch, offsetMinutes).map(({ t, date }) => ({ t, date, plays: counts.get(date) || 0 }));
  if (!cells.length) return null;

  const startRow = (new Date(cells[0].t).getUTCDay() + 6) % 7; // weeks start on Monday
  const weeks = Math.floor((cells.length - 1 + startRow) / 7) + 1;
  const gutter = 30;
  const header = 18;
  const gap = 3;
  const size = clamp(Math.floor((width - gutter) / weeks) - gap, 6, 16);
  const step = size + gap;
  const height = header + 7 * step;
  cells.forEach((cell, i) => {
    cell.col = Math.floor((i + startRow) / 7);
    cell.row = (i + startRow) % 7;
  });

  const played = cells.map((c) => c.plays).filter((p) => p > 0).sort((a, b) => a - b);
  const cuts = [0.25, 0.5, 0.75].map((q) => played[Math.min(played.length - 1, Math.floor(played.length * q))]);
  const level = (plays) => (plays === 0 ? 0 : plays <= cuts[0] ? 1 : plays <= cuts[1] ? 2 : plays <= cuts[2] ? 3 : 4);

  const months = [];
  cells.forEach((cell) => {
    const d = new Date(cell.t);
    if (d.getUTCDate() <= 7 && cell.row === 0 || (cell === cells[0])) {
      const label = d.toLocaleString(undefined, { month: 'short', timeZone: 'UTC' });
      if (!months.length || months[months.length - 1].label !== label) months.push({ label, col: cell.col });
    }
  });

  const cellAt = (event) => {
    const rect = box.current.getBoundingClientRect();
    const col = Math.floor((event.clientX - rect.left - gutter) / step);
    const row = Math.floor((event.clientY - rect.top - header) / step);
    if (col < 0 || row < 0 || row > 6) return null;
    const found = cells.findIndex((c) => c.col === col && c.row === row);
    return found < 0 ? null : found;
  };
  const onKey = (event) => {
    const moves = { ArrowRight: 7, ArrowLeft: -7, ArrowDown: 1, ArrowUp: -1 };
    if (event.key in moves) setActive(clamp((active ?? (moves[event.key] > 0 ? -1 : cells.length)) + moves[event.key], 0, cells.length - 1));
    else if (event.key === 'Home') setActive(0);
    else if (event.key === 'End') setActive(cells.length - 1);
    else if (event.key === 'Escape') setActive(null);
    else return;
    event.preventDefault();
  };
  const shown = active !== null ? cells[active] : null;
  const below = shown !== null && header + shown.row * step < TIP_HEIGHT; // the first rows have no room above them
  const when = (cell) => new Date(cell.t).toLocaleDateString(undefined, { weekday: 'short', year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC' });
  const text = (cell) => counted(cell.plays, unit);

  return html`<div class="viz-plot heat" ref=${box} tabIndex="0" role="group" aria-roledescription="chart" aria-label="Plays on each day"
      onPointerMove=${(e) => setActive(cellAt(e))} onPointerLeave=${() => setActive(null)} onKeyDown=${onKey} onBlur=${() => setActive(null)}>
    <svg width=${width} height=${height} viewBox="0 0 ${width} ${height}" aria-hidden="true">
      <g class="viz-axis-labels">
        ${months.map((m) => html`<text key=${m.label + m.col} x=${gutter + m.col * step} y=${11}>${m.label}</text>`)}
        ${WEEKDAYS.map((w, row) => w && html`<text key=${w} x=${0} y=${header + row * step + size - 2}>${w}</text>`)}
      </g>
      <g>
        ${cells.map((c, i) => html`<rect key=${c.date} class="cell lv${level(c.plays)} ${active === i ? 'hot' : ''}" x=${gutter + c.col * step} y=${header + c.row * step} width=${size} height=${size} rx="2" />`)}
      </g>
    </svg>
    ${shown && html`<div class=${`viz-tip ${below ? 'below' : ''}`} style=${{ left: clamp(gutter + shown.col * step + size / 2, 80, width - 80) + 'px', top: (below ? header + (shown.row + 1) * step + 4 : header + shown.row * step - 6) + 'px' }}>
      <strong>${text(shown)}</strong><span>${when(shown)}</span>
    </div>`}
    <div class="viz-scale" aria-hidden="true"><span>Fewer</span>${[0, 1, 2, 3, 4].map((l) => html`<i key=${l} class="cell lv${l}"></i>`)}<span>More</span></div>
    <span class="sr-only" role="status" aria-live="polite">${shown ? `${when(shown)}: ${text(shown)}` : ''}</span>
  </div>`;
}

// ------------------------------------------------------------ ranked bars

/** items: [{ key, label, sub, value, display, onClick, href }]. The bar is the share of the largest value. */
export function RankedBars({ items, ariaLabel }) {
  const biggest = Math.max(1, ...items.map((i) => i.value));
  return html`<ol class="viz viz-ranked" aria-label=${ariaLabel}>
    ${items.map((item, i) => html`<li key=${item.key ?? i}>
      <span class="rk-name">
        ${item.onClick
          ? html`<button type="button" class="rk-play" onClick=${item.onClick} title=${item.playTitle || 'Play'}>${item.label}</button>`
          : item.href ? html`<a href=${item.href}>${item.label}</a>` : html`<span>${item.label}</span>`}
        ${item.sub && html`<small class="subtle">${item.sub}</small>`}
      </span>
      <span class="rk-track" aria-hidden="true"><span class="rk-bar" style=${{ width: Math.max(2, (item.value / biggest) * 100) + '%' }}></span></span>
      <span class="rk-val">${item.display ?? item.value.toLocaleString()}</span>
    </li>`)}
  </ol>`;
}
