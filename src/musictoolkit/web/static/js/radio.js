// Radio from the page's side: keeping a station, playing it, and its favorite mark. (The screens are in views/radio.js.)
import { api, createStore } from './lib.js';
import { playQueue, updateItem } from './player.js';
import { notifyError } from './state.js';

/** Counts the changes to the stations kept (one favorited from the player bar, one kept by playing it...), so that a screen that lists them reads them again. */
export const stations = createStore({ rev: 0 });
const changed = () => stations.set((s) => ({ rev: s.rev + 1 }));

/** The directory's entries carry no `kind`; the player and the pictures need it. */
export const asStation = (station) => ({ ...station, kind: 'station' });

/**
 * A station from the directory has no number here until it is kept. This keeps it (its favorite mark stays as it is unless `favorite` says
 * otherwise) and returns the kept station; a station that is kept already is returned as it is.
 */
export async function keepStation(station, { favorite = Boolean(station.favorite) } = {}) {
  if (station.id && favorite === Boolean(station.favorite)) return station;
  const kept = await (station.id ? api(`/radio/stations/${station.id}`, { method: 'PATCH', body: { favorite } }) : save(station, favorite));
  changed();
  return kept;
}

function save(station, favorite) {
  return api('/radio/stations/save', {
    method: 'POST',
    body: {
      name: station.name,
      stream_url: station.stream_url,
      source: station.source === 'manual' ? 'manual' : 'radio-browser',
      source_id: station.source_id || null,
      homepage: station.homepage || null,
      art_url: station.art_url || null,
      tags: station.tags || [],
      country: station.country || null,
      country_code: station.country_code || null,
      language: station.language || null,
      codec: station.codec || null,
      bitrate: station.bitrate || null,
      favorite,
    },
  });
}

/** Play a station now (a station from the directory is kept first, so that it has somewhere to be played from). */
export async function playStation(station, source = 'Radio') {
  try {
    const kept = await keepStation(station);
    await playQueue([kept], 0, { source });
    return kept;
  } catch (error) {
    notifyError(error);
    return null;
  }
}

/** Mark a station a favorite or not (keeping it first if it came from the directory). Returns the kept station. */
export async function setStationFavorite(station, favorite) {
  const kept = await keepStation(station, { favorite });
  updateItem('station', kept.id, { favorite: kept.favorite });
  return kept;
}
