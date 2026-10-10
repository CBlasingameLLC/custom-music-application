// Tag editing: the Edit tags dialog, and the "saved / undo" feedback when its job finishes.
import { api, fmt, html, useEffect, useRef, useState, useStore } from './lib.js';
import { Icon } from './icons.js';
import { Button, Spinner } from './components.js';
import { desktop, library, notifyError, onJobFinished, openModal, toast, trackJob } from './state.js';

const FIELDS = [
  { key: 'title', label: 'Title', single: true, wide: true },
  { key: 'artist', label: 'Artist' },
  { key: 'album_artist', label: 'Album artist' },
  { key: 'album', label: 'Album', wide: true },
  { key: 'genre', label: 'Genre', list: 'tag-genres' },
  { key: 'year', label: 'Year', number: true },
  { key: 'track_number', label: 'Track number', number: true, single: true },
  { key: 'disc_number', label: 'Disc number', number: true },
];

function TagEditor({ ids, close }) {
  const { facets } = useStore(library);
  const [data, setData] = useState(null);
  const [values, setValues] = useState({});
  const [touched, setTouched] = useState({});
  const [saving, setSaving] = useState(false);
  const first = useRef();

  useEffect(() => {
    api('/tags/read', { method: 'POST', body: { ids } })
      .then((read) => {
        setData(read);
        setValues(Object.fromEntries(FIELDS.map((f) => [f.key, read.fields[f.key].mixed ? '' : String(read.fields[f.key].value ?? '')])));
      })
      .catch((error) => { notifyError(error); close(); });
  }, []);
  useEffect(() => {
    // Start in the first field, unless the person already clicked into another one: stealing the focus back
    // would swallow whatever they had just started typing there.
    if (data && !document.activeElement?.closest?.('.tag-form')) first.current?.focus();
  }, [!!data]);

  if (!data) return html`<${Spinner} />`;
  const single = data.count === 1;
  const visible = FIELDS.filter((f) => single || !f.single);

  // Only what the person actually changed is sent; an untouched field keeps each song's own value.
  const changes = {};
  for (const f of visible) {
    if (!touched[f.key]) continue;
    const initial = data.fields[f.key];
    const text = String(values[f.key] ?? '').trim();
    if (initial.mixed || text !== String(initial.value ?? '')) changes[f.key] = text;
  }
  const changed = Object.keys(changes).length;

  async function save(event) {
    event.preventDefault();
    if (!changed || saving) return;
    setSaving(true);
    try {
      const { job } = await api('/tags/edit', { method: 'POST', body: { ids, changes } });
      trackJob(job);
      close();
    } catch (error) {
      notifyError(error);
      setSaving(false);
    }
  }

  const set = (key, value) => {
    setValues((v) => ({ ...v, [key]: value }));
    setTouched((t) => ({ ...t, [key]: true }));
  };

  return html`<form class="tag-form" onSubmit=${save}>
    <p class="subtle tag-sub">
      ${single
        ? html`<span class="path">${data.path}</span>`
        : `${fmt.plural(data.count, 'song')}. Fields marked “Multiple values” keep each song's own value unless you type something.`}
    </p>
    <div class="tag-grid">
      ${visible.map((f, i) => {
        const initial = data.fields[f.key];
        return html`<label class="field ${f.wide ? 'wide' : ''}" key=${f.key}>
          <span>${f.label}</span>
          <input ref=${i === 0 ? first : null} type=${f.number ? 'number' : 'text'} inputmode=${f.number ? 'numeric' : undefined} min=${f.number ? (f.key === 'year' ? 1 : 0) : undefined}
            list=${f.list} value=${values[f.key] ?? ''} placeholder=${initial.mixed ? 'Multiple values' : ''} autocomplete="off"
            onInput=${(e) => set(f.key, e.target.value)} />
        </label>`;
      })}
    </div>
    <datalist id="tag-genres">${(facets?.genres || []).slice(0, 80).map((g) => html`<option value=${g.name}></option>`)}</datalist>
    ${data.unwritable.length > 0 && html`<p class="note warn"><${Icon} name="alert" size=${15} />
      ${data.unwritable.length === data.count ? 'These songs are' : `${fmt.plural(data.unwritable.length, 'song')} here ${data.unwritable.length === 1 ? 'is' : 'are'}`}
      in a format whose tags can't be edited yet (${[...new Set(data.unwritable.map((u) => (u.format || '?').toUpperCase()))].join(', ')}); ${data.unwritable.length === data.count ? 'saving will skip them' : 'they will be skipped'}.</p>`}
    <p class="note"><${Icon} name="info" size=${15} /> Changes are written into the music files themselves. You can undo the last edits right after saving.</p>
    <div class="modal-actions">
      ${single && desktop?.showItemInFolder && html`<${Button} icon="folder" onClick=${() => desktop.showItemInFolder(data.path)}>Show file<//>`}
      <span class="spacer"></span>
      <${Button} onClick=${close}>Cancel<//>
      <${Button} kind="primary" type="submit" icon="check" disabled=${!changed || saving}>${saving ? 'Saving…' : 'Save'}<//>
    </div>
  </form>`;
}

/** Open the editor for these song ids (one song or many). */
export function openTagEditor(ids) {
  const unique = [...new Set(ids)];
  if (!unique.length) return;
  openModal({ title: unique.length === 1 ? 'Edit tags' : `Edit tags for ${unique.length} songs`, width: 600, render: (close) => html`<${TagEditor} ids=${unique} close=${close} />` });
}

// ------------------------------------------------------------ feedback when the job finishes

export async function undoTagEdit(batchId) {
  try {
    const { job } = await api('/tags/undo', { method: 'POST', body: { batch_id: batchId } });
    trackJob(job);
  } catch (error) {
    notifyError(error);
  }
}

onJobFinished((job) => {
  if (job.kind !== 'tags' || job.status !== 'done') return;
  const { edited = 0, unchanged = 0, errors = [], batch_id: batchId, undo } = job.result || {};
  if (undo) {
    toast(`Undone: ${fmt.plural(edited, 'song')} restored`, 'success');
  } else if (edited) {
    toast(`Updated tags on ${fmt.plural(edited, 'song')}`, 'success', 12000, { label: 'Undo', onClick: () => undoTagEdit(batchId) });
  } else if (!errors.length) {
    toast(unchanged ? 'Nothing needed changing: the tags already matched.' : 'Nothing to save.');
  }
  if (errors.length) {
    const sample = errors.slice(0, 2).map((e) => `${e.title || 'A song'}: ${e.error}`).join(' · ');
    toast(`${fmt.plural(errors.length, 'song')} could not be changed. ${sample}${errors.length > 2 ? ' …' : ''}`, 'error', 14000);
  }
});
