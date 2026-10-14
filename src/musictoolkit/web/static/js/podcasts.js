// Podcasts from the page's side: playing episodes, marking them heard, checking for new ones. (The screens are in views/podcasts.js.)
import { api } from './lib.js';
import { addToQueue, playQueue, updateItem } from './player.js';
import { notifyError, toast, trackJob } from './state.js';

/** An episode as the player wants it: where listening stopped is read afresh, because the copy a list holds may be old. */
async function fresh(episode) {
  try {
    return await api(`/podcasts/episodes/${episode.id}`);
  } catch {
    return episode;
  }
}

/** Play `episodes[index]`, then the ones after it. */
export async function playEpisodes(episodes, index = 0, source = 'Podcasts') {
  if (!episodes[index]) return;
  try {
    const list = episodes.slice();
    list[index] = await fresh(list[index]);
    await playQueue(list, index, { source });
  } catch (error) {
    notifyError(error);
  }
}

/** Put an episode in the queue, after the one playing (`next`) or at the end. */
export async function queueEpisode(episode, { next = false } = {}) {
  addToQueue([await fresh(episode)], { next });
  toast(next ? `"${episode.title}" plays next` : `Added "${episode.title}" to the queue`, 'success');
}

/** Mark an episode heard or not heard. Returns the episode as the app now has it. */
export async function setHeard(episode, played) {
  const kept = await api(`/podcasts/episodes/${episode.id}/played`, { method: 'POST', body: { played } });
  updateItem('episode', episode.id, { played: kept.played, position: kept.position, new: kept.new });
  return kept;
}

/** Ask the feeds of every podcast followed (or of one) whether there is anything new; it runs as a background task. */
export async function checkForNewEpisodes(podcastId) {
  try {
    const { job } = await api(podcastId ? `/podcasts/${podcastId}/refresh` : '/podcasts/refresh', { method: 'POST' });
    trackJob(job);
    toast('Checking for new episodes…');
  } catch (error) {
    if (error.status === 409) toast('The podcasts are being checked already.');
    else notifyError(error);
  }
}
