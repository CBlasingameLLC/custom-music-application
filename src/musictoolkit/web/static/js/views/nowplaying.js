// Now Playing: big cover, synced lyrics, the queue, and the sound controls.
import { api, fmt, html, useAsync, useEffect, useMemo, useRef, useState, useStore } from '../lib.js';
import { Icon } from '../icons.js';
import { Button, Cover, Empty, IconButton } from '../components.js';
import { confirmDialog, promptText } from '../dialogs.js';
import { clearQueue, current, EQ_BANDS, EQ_PRESETS, getAnalyser, jumpTo, moveInQueue, player, removeFromQueue, seek, setEq, setNormalize, setSleepTimer } from '../player.js';
import { enc, go, href, loadPlaylists, notifyError, toast } from '../state.js';

// ------------------------------------------------------------ queue

export function QueueList() {
  const { items, order, pos, playing, source } = useStore(player);
  const dragFrom = useRef(null);
  const [dropAt, setDropAt] = useState(null);
  const currentRow = useRef();
  useEffect(() => currentRow.current?.scrollIntoView({ block: 'nearest' }), [pos, items.length]);

  if (!order.length) {
    return html`<${Empty} icon="queue" title="The queue is empty">Play something, or add songs with “Add to queue”.<//>`;
  }

  async function saveAsPlaylist() {
    const name = await promptText({ title: 'Save queue as playlist', label: 'Name', value: source ? source.replace(/^[^:]+: /, '') : '', confirm: 'Save' });
    if (!name) return;
    try {
      const playlist = await api('/playlists', { method: 'POST', body: { name, track_ids: order.map((i) => items[i].id) } });
      toast(`Saved ${fmt.plural(order.length, 'song')} as "${playlist.name}"`, 'success');
      loadPlaylists();
      go(`/playlist/${playlist.id}`);
    } catch (error) { notifyError(error); }
  }

  const row = (orderIndex) => {
    const track = items[order[orderIndex]];
    const isCurrent = orderIndex === pos;
    return html`<div class="qrow ${isCurrent ? 'current' : ''} ${orderIndex < pos ? 'played' : ''} ${dropAt === orderIndex ? 'drop-before' : ''}" key=${orderIndex + ':' + track.id}
      ref=${isCurrent ? currentRow : null} draggable="true"
      onDragStart=${(e) => { dragFrom.current = orderIndex; e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', String(orderIndex)); }}
      onDragOver=${(e) => { e.preventDefault(); setDropAt(orderIndex); }}
      onDragEnd=${() => { dragFrom.current = null; setDropAt(null); }}
      onDrop=${(e) => { e.preventDefault(); const from = dragFrom.current; dragFrom.current = null; setDropAt(null); if (from !== null) moveInQueue(from, orderIndex); }}
      onDblClick=${() => jumpTo(orderIndex)}>
      <span class="qgrip"><${Icon} name="grip" size=${14} /></span>
      <button class="qplay" aria-label="Play ${track.title}" onClick=${() => jumpTo(orderIndex)}>
        <${Cover} trackId=${track.id} name=${track.album} size=40 />
        <span class="qplay-overlay">${isCurrent && playing ? html`<span class="eq-bars"><i></i><i></i><i></i></span>` : html`<${Icon} name="play" size=${15} />`}</span>
      </button>
      <div class="qtext"><span class="t1">${track.title}</span><span class="t2">${track.artist}</span></div>
      <span class="qtime">${fmt.time(track.duration)}</span>
      <button class="icon-btn" aria-label="Remove from queue" onClick=${() => removeFromQueue(orderIndex)}><${Icon} name="x" size=${15} /></button>
    </div>`;
  };

  const upcoming = order.length - pos - 1;
  return html`<div class="queue">
    <div class="queue-head">
      <span class="subtle">${source ? `Playing from ${source}` : 'Queue'} · ${fmt.plural(order.length, 'song')}${upcoming > 0 ? `, ${upcoming} up next` : ''}</span>
      <span class="spacer"></span>
      <button class="chip-btn" onClick=${saveAsPlaylist}><${Icon} name="plus" size=${14} /> Save as playlist</button>
      <button class="chip-btn" onClick=${async () => (order.length < 5 || (await confirmDialog({ title: 'Clear the queue', message: `Remove all ${order.length} songs from the queue?`, confirm: 'Clear', danger: true }))) && clearQueue()}><${Icon} name="trash" size=${14} /> Clear</button>
    </div>
    <div class="queue-rows">${order.map((_, i) => row(i))}</div>
  </div>`;
}

// ------------------------------------------------------------ lyrics

function activeLine(lines, position) {
  let lo = 0;
  let hi = lines.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (lines[mid].t <= position + 0.15) { found = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return found;
}

function Lyrics({ track }) {
  const { data, loading } = useAsync(() => api(`/tracks/${track.id}/lyrics`), [track.id]);
  const position = useStore(player, (s) => s.position);
  const box = useRef();
  const userScrolledAt = useRef(0);
  const synced = data?.synced;
  const active = synced ? activeLine(synced, position) : -1;

  useEffect(() => {
    if (active < 0 || !box.current || Date.now() - userScrolledAt.current < 4000) return;
    const line = box.current.children[active];
    if (line) box.current.scrollTo({ top: line.offsetTop - box.current.clientHeight / 2 + line.clientHeight / 2, behavior: 'smooth' });
  }, [active]);

  if (loading && !data) return html`<p class="subtle pad">Looking for lyrics…</p>`;
  if (synced) {
    return html`<div class="lyrics synced" ref=${box} onWheel=${() => (userScrolledAt.current = Date.now())}>
      ${synced.map((line, i) => html`<p class="${i === active ? 'active' : i < active ? 'past' : ''}" onClick=${() => seek(line.t)}>${line.text || '♪'}</p>`)}
    </div>`;
  }
  if (data?.plain) return html`<div class="lyrics plain"><pre>${data.plain}</pre></div>`;
  return html`<${Empty} icon="mic" title="No lyrics for this song">
    <p>Lyrics are read from the file's tags, or from a <code>.lrc</code> file with the same name next to it (that is how timed, scrolling lyrics work).</p>
  <//>`;
}

// ------------------------------------------------------------ visualizer

function Visualizer() {
  const canvas = useRef();
  const playing = useStore(player, (s) => s.playing);
  useEffect(() => {
    const el = canvas.current;
    if (!el) return undefined;
    const ctx2d = el.getContext('2d');
    const analyser = getAnalyser();
    const data = new Uint8Array(analyser.frequencyBinCount);
    let frame;
    const bars = 56;
    const draw = () => {
      const dpr = window.devicePixelRatio || 1;
      const { clientWidth: w, clientHeight: h } = el;
      if (el.width !== w * dpr) { el.width = w * dpr; el.height = h * dpr; }
      ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx2d.clearRect(0, 0, w, h);
      analyser.getByteFrequencyData(data);
      const gap = 3;
      const barWidth = (w - gap * (bars - 1)) / bars;
      const style = getComputedStyle(document.documentElement);
      const gradient = ctx2d.createLinearGradient(0, h, 0, 0);
      gradient.addColorStop(0, style.getPropertyValue('--accent-strong').trim() || '#666dff');
      gradient.addColorStop(1, style.getPropertyValue('--accent-2').trim() || '#c07bff');
      ctx2d.fillStyle = gradient;
      for (let i = 0; i < bars; i++) {
        // log-spaced bins so bass doesn't hog the whole display
        const start = Math.floor(Math.pow(i / bars, 2.2) * data.length * 0.75);
        const end = Math.max(start + 1, Math.floor(Math.pow((i + 1) / bars, 2.2) * data.length * 0.75));
        let peak = 0;
        for (let j = start; j < end; j++) peak = Math.max(peak, data[j]);
        const barHeight = Math.max(3, (peak / 255) * h);
        const x = i * (barWidth + gap);
        const radius = Math.min(barWidth / 2, 4);
        ctx2d.beginPath();
        ctx2d.roundRect(x, h - barHeight, barWidth, barHeight, [radius, radius, 0, 0]);
        ctx2d.fill();
      }
      frame = requestAnimationFrame(draw);
    };
    draw();
    return () => cancelAnimationFrame(frame);
  }, [playing]);
  return html`<canvas class="visualizer" ref=${canvas} aria-hidden="true"></canvas>`;
}

// ------------------------------------------------------------ sound controls

function SoundPanel() {
  const { eq, normalize, sleepAt } = useStore(player);
  const [remaining, setRemaining] = useState('');
  useEffect(() => {
    if (!sleepAt) return setRemaining('');
    const tick = () => setRemaining(fmt.time(Math.max(0, (sleepAt - Date.now()) / 1000)));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [sleepAt]);
  const labels = EQ_BANDS.map((f) => (f >= 1000 ? `${f / 1000}k` : f));
  const preset = Object.keys(EQ_PRESETS).find((name) => EQ_PRESETS[name].every((g, i) => g === eq.gains[i])) || 'Custom';

  return html`<div class="sound">
    <h3>Equalizer</h3>
    <div class="sound-row">
      <label class="switch"><input type="checkbox" checked=${eq.enabled} onChange=${(e) => setEq({ enabled: e.target.checked })} /><span>On</span></label>
      <select value=${preset} aria-label="Equalizer preset" onChange=${(e) => EQ_PRESETS[e.target.value] && setEq({ preset: e.target.value, enabled: true })}>
        ${[...Object.keys(EQ_PRESETS), 'Custom'].map((name) => html`<option value=${name}>${name}</option>`)}
      </select>
    </div>
    <div class="eq ${eq.enabled ? '' : 'off'}">
      ${EQ_BANDS.map((f, i) => html`<label class="eq-band">
        <span class="db">${eq.gains[i] > 0 ? '+' : ''}${eq.gains[i]}</span>
        <input type="range" min="-12" max="12" step="1" value=${eq.gains[i]} aria-label="${labels[i]} Hz"
          onInput=${(e) => { const gains = eq.gains.slice(); gains[i] = Number(e.target.value); setEq({ gains, enabled: true, preset: 'Custom' }); }} />
        <span class="hz">${labels[i]}</span>
      </label>`)}
    </div>
    <h3>Volume</h3>
    <label class="switch"><input type="checkbox" checked=${normalize} onChange=${(e) => setNormalize(e.target.checked)} />
      <span>Even out loudness between songs (ReplayGain)</span></label>
    <p class="subtle">Uses the loudness tags stored in your files. Songs without them play unchanged.</p>
    <h3>Sleep timer</h3>
    <div class="sound-row">
      <select value=${sleepAt ? 'on' : 'off'} aria-label="Sleep timer" onChange=${(e) => setSleepTimer(e.target.value === 'off' ? 0 : Number(e.target.value))}>
        <option value="off">${sleepAt ? `Stopping in ${remaining}` : 'Off'}</option>
        ${[15, 30, 45, 60, 90].map((m) => html`<option value=${m}>Stop after ${m} minutes</option>`)}
      </select>
      ${sleepAt && html`<${Button} small onClick=${() => setSleepTimer(0)}>Cancel<//>`}
    </div>
  </div>`;
}

// ------------------------------------------------------------ the view

export function NowPlayingView() {
  const track = useStore(player, (s) => current(s));
  const [tab, setTab] = useState('lyrics');
  if (!track) {
    return html`<${Empty} icon="music" title="Nothing is playing">Pick a song from your library to start. Lyrics, the queue and the equalizer live here.<//>`;
  }
  return html`<div class="nowplaying">
    <div class="np-left">
      <${Cover} albumKey=${track.album_key} name=${track.album} size=480 class="np-art" />
      <div class="np-meta">
        <h1>${track.title}</h1>
        <p><a href=${href(`/artist/${enc(track.album_artist || track.artist)}`)}>${track.artist}</a> · <a href=${href(`/album/${track.album_key}`)}>${track.album}</a></p>
      </div>
      <${Visualizer} />
    </div>
    <div class="np-right">
      <div class="tabs" role="tablist">
        ${[['lyrics', 'Lyrics', 'mic'], ['queue', 'Up next', 'queue'], ['sound', 'Sound', 'sliders']].map(([id, label, icon]) => html`
          <button role="tab" class="tab ${tab === id ? 'active' : ''}" aria-selected=${tab === id} onClick=${() => setTab(id)}><${Icon} name=${icon} size=${15} /> ${label}</button>`)}
      </div>
      <div class="tab-body">
        ${tab === 'lyrics' && html`<${Lyrics} track=${track} />`}
        ${tab === 'queue' && html`<${QueueList} />`}
        ${tab === 'sound' && html`<${SoundPanel} />`}
      </div>
    </div>
  </div>`;
}
