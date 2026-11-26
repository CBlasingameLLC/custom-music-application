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

/**
 * What Settings says about how this device signs in, from what `GET /api/session` answered: `{ by, tailscale: { on, login, allowed, own_page } }`
 * (`by`: 'token', 'tailscale' or 'open'; `tailscale.login`: the account Tailscale says this device belongs to, if it says; `on`: the
 * server signs in some accounts; `allowed`: this one is among them). The answer's `state` is
 *   'open'      the app has no token: there is nothing to say;
 *   'tailscale' Tailscale signs this device in as `login`, with no token;
 *   'unlisted'  Tailscale names `login`, the server signs in other accounts, not this one (`entry` is how to write it in the list);
 *   'offer'     Tailscale names `login` and the server signs in none: `line` is what would make it sign this one in;
 *   'remembered' Tailscale signed this device in earlier (its cookie), and names no one in this visit;
 *   'token'     this device is signed in with the token, and Tailscale named no one.
 */
export function signInAdvice(session) {
  if (!session || session.by === 'open') return { state: 'open' };
  const { on, login, allowed } = session.tailscale || {};
  if (!login) return { state: session.by === 'tailscale' ? 'remembered' : 'token', by: session.by };
  const entry = JSON.stringify(login); // (a quoted string of TOML, as the server's own page writes it)
  if (on && allowed) return { state: 'tailscale', by: session.by, login };
  if (on) return { state: 'unlisted', by: session.by, login, entry };
  return { state: 'offer', by: session.by, login, entry, line: `tailscale_logins = [${entry}]` };
}

/** Is this page open as an installed app? (iOS says so with `navigator.standalone`, other browsers with the display mode.) */
export const openedAsApp = () => Boolean(navigator.standalone) || Boolean(window.matchMedia?.('(display-mode: standalone)').matches);
