// What to tell someone about putting the app on a phone or a Mac, from how this page was opened. Pure: it needs no page.

/**
 * `where` is { protocol, hostname, standalone }: the address the page was opened at, and whether it was opened from an icon on a
 * Home Screen or in a Dock (`standalone`) rather than in a browser's tab.
 */
export function installAdvice({ protocol, hostname, standalone }) {
  return {
    installed: Boolean(standalone),
    // an address that only works on the computer it is typed on: a phone cannot be given it
    local: ['localhost', '127.0.0.1', '[::1]', '::1'].includes(hostname) || hostname.endsWith('.localhost'),
    // a phone only gives a Home Screen app its own worker (and the page that says the server cannot be reached) over https
    secure: protocol === 'https:',
  };
}

/** Is this page open as an installed app? (iOS says so with `navigator.standalone`, other browsers with the display mode.) */
export const openedAsApp = () => Boolean(navigator.standalone) || Boolean(window.matchMedia?.('(display-mode: standalone)').matches);
