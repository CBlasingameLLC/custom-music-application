'use strict';

// What the tray icon says and offers. Pure, so the wording and the enabled states are tested (test/tray-menu.test.js).

const MAX_TOOLTIP = 120; // Windows shows at most 127 characters of a tray tooltip

/** "Music Toolkit", or what is playing / paused. Plain text: no markup is interpreted in a tooltip. */
function tooltipText({ title, artist, playing } = {}) {
  if (!title) return 'Music Toolkit';
  const text = `${playing ? 'Playing' : 'Paused'}: ${title}${artist ? ` - ${artist}` : ''}`;
  return text.length > MAX_TOOLTIP ? `${text.slice(0, MAX_TOOLTIP - 3)}...` : text;
}

/** Menu items for Electron's Menu.buildFromTemplate. `actions` supplies toggleWindow, playPause, next, previous, quit. */
function trayMenuTemplate({ windowVisible, playing, hasTrack }, actions) {
  return [
    { label: windowVisible ? 'Hide Music Toolkit' : 'Show Music Toolkit', click: actions.toggleWindow },
    { type: 'separator' },
    { label: playing ? 'Pause' : 'Play', enabled: Boolean(hasTrack), click: actions.playPause },
    { label: 'Next', enabled: Boolean(hasTrack), click: actions.next },
    { label: 'Previous', enabled: Boolean(hasTrack), click: actions.previous },
    { type: 'separator' },
    { label: 'Quit Music Toolkit', click: actions.quit },
  ];
}

module.exports = { MAX_TOOLTIP, tooltipText, trayMenuTemplate };
