// Runs before the page is drawn: puts the saved theme, density and accent colours on the page, so someone who chose the
// light theme or another accent never sees the defaults flash first. (state.js sets the same things again, from the same
// saved choices, once the app has started.) A classic script, not a module, so the browser waits for it.
//
// It also takes the token out of the address a link that signs in was opened at: the server has given the cookie by now, and the
// address bar, a bookmark and a screenshot should not have the token in them. (Not in the desktop window, whose address is how the
// shell and its checks know the window is the app's.)
(function () {
  try {
    var params = new URLSearchParams(location.search);
    if (!window.mtk && params.has('token')) {
      params.delete('token');
      var rest = params.toString();
      history.replaceState(null, '', location.pathname + (rest ? '?' + rest : '') + location.hash);
    }
  } catch (error) {
    /* an address that cannot be changed stays as it is */
  }
  try {
    var saved = JSON.parse(localStorage.getItem('mtk.ui.v1') || '{}');
    var root = document.documentElement;
    if (saved.theme === 'light' || saved.theme === 'dark') root.setAttribute('data-theme', saved.theme);
    root.setAttribute('data-density', saved.density === 'compact' ? 'compact' : 'comfortable');
    var vars = saved.vars || {};
    for (var name in vars) {
      var value = String(vars[name]);
      if (/^--(accent[a-z0-9-]*|on-accent)$/.test(name) && /^(#[0-9a-f]{3,8}|rgba?\([0-9., ]+\))$/i.test(value)) root.style.setProperty(name, value);
    }
  } catch (error) {
    /* storage may be unavailable: the app starts with its defaults */
  }
})();
