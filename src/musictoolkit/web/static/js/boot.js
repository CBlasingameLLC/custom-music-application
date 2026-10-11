// Runs before the page is drawn: puts the saved theme, density and accent colours on the page, so someone who chose the
// light theme or another accent never sees the defaults flash first. (state.js sets the same things again, from the same
// saved choices, once the app has started.) A classic script, not a module, so the browser waits for it.
(function () {
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
