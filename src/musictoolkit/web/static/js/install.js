// What to tell someone about putting the app on a phone or a Mac, from how this page was opened. Pure: it needs no page.

/**
 * `where` is { protocol, hostname, standalone }: the address the page was opened at, and whether it was opened from an icon on a
 * Home Screen or in a Dock (`standalone`) rather than in a browser's tab. The answer says whether it is `installed`, and which `note`
 * the page owes: 'local' (the address works only on the computer it is typed on, so a phone cannot be given it), 'http' (a phone can
 * still put it on its Home Screen, but a Home Screen app only gets its own worker, and with it the page that says the server cannot be
 * reached, over https) or '' (nothing to say).
 */
export function installAdvice({ protocol, hostname, standalone }) {
  const local = ['localhost', '127.0.0.1', '[::1]'].includes(hostname) || hostname.endsWith('.localhost');
  const secure = protocol === 'https:';
  return { installed: Boolean(standalone), local, secure, note: local ? 'local' : secure ? '' : 'http' };
}

/** Is this page open as an installed app? (iOS says so with `navigator.standalone`, other browsers with the display mode.) */
export const openedAsApp = () => Boolean(navigator.standalone) || Boolean(window.matchMedia?.('(display-mode: standalone)').matches);
