// Runs on the page that asks for the token (see remember.py). A device that was signed in, and whose browser has lost the cookie, still has the key that the app
// saved in this page's storage: this tries it before anyone has to type the token. A key that is no good is thrown away. A classic script, with nothing of the app
// in it: the page that asks for the token is a page of its own.
(function () {
  var NAME = 'mtk.remember.v1'; // (the name remember.js keeps the key under)
  var TRIED = 'mtk.restoring'; // (the second the key was last tried, in this window)
  var PAUSE = 30000; // milliseconds in which a key is not tried again
  var note = document.getElementById('restore-note');

  function say(text) {
    if (!note) return;
    note.textContent = text;
    note.hidden = false;
  }
  function forget() {
    try {
      localStorage.removeItem(NAME);
    } catch (error) {
      /* storage that cannot be written cannot be cleared either */
    }
  }

  var saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(NAME) || 'null');
  } catch (error) {
    return; // storage is blocked, or holds something else: the form is all there is
  }
  if (!saved || typeof saved.key !== 'string' || !saved.key) return;
  if (typeof saved.expires === 'number' && saved.expires * 1000 <= Date.now()) {
    forget();
    say('The key this device saved has run out. Type the token.');
    return;
  }

  // One try each time the page is opened: if the cookie that the key buys is not kept either, the page must not go round again and again.
  var last = 0;
  try {
    last = Number(sessionStorage.getItem(TRIED)) || 0;
    if (Date.now() - last < PAUSE) {
      say('The key this device saved was tried a moment ago, and the browser did not keep the sign-in it gave. Type the token.');
      return;
    }
    sessionStorage.setItem(TRIED, String(Date.now()));
  } catch (error) {
    return; // with no way to remember that it tried, it does not try
  }

  say('Signing this device in with the key it saved…');
  fetch('/signin/restore', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: 'key=' + encodeURIComponent(saved.key),
  })
    .then(function (response) {
      if (response.status === 204) {
        location.replace('/');
        return;
      }
      if (response.status === 429) {
        say('Too many wrong tries. Wait a minute, or type the token.');
        return;
      }
      if (response.status === 401 || response.status === 404) {
        forget();
        say("The key this device saved is no good any more (it ran out, or the server's token was changed). Type the token.");
        return;
      }
      say('The server did not take the saved key. Type the token.');
    })
    .catch(function () {
      say('The server could not be reached with the saved key. Type the token, or open the page again.');
    });
})();
