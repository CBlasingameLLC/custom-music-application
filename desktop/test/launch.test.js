'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const nodePath = require('node:path');
const { AUDIO_EXTENSIONS, MAX_ARGUMENT_PATHS, filesFromArgv } = require('../src/main/launch.js');

// A pretend file system: path -> 'file' | 'directory'.
const kinds = (table) => (file) => table[file] || null;

test('the installer registers exactly the formats the player can decode', () => {
  assert.deepEqual(AUDIO_EXTENSIONS, ['.mp3', '.flac', '.m4a', '.aac', '.ogg', '.oga', '.opus', '.wav']);
});

test('a song handed to the installed program on Windows is found', () => {
  const song = 'C:\\Users\\me\\Music\\Song.mp3';
  const found = filesFromArgv(['C:\\Program Files\\Music Toolkit\\Music Toolkit.exe', song], { path: nodePath.win32, cwd: 'C:\\Windows', kind: kinds({ [song]: 'file' }) });
  assert.deepEqual(found, [song]);
});

test('running as "electron ." skips the app folder as well', () => {
  const found = filesFromArgv(['/usr/bin/electron', '.', 'song.flac', '/music/other.opus'], {
    defaultApp: true, cwd: '/home/me', path: nodePath.posix, kind: kinds({ '/home/me/song.flac': 'file', '/music/other.opus': 'file', '/home/me': 'directory' }),
  });
  assert.deepEqual(found, ['/home/me/song.flac', '/music/other.opus']);
});

test('a switch in front of the app folder does not turn the folder into something to open', () => {
  // `electron --no-sandbox . song.mp3`: the app folder is the first argument that is not a switch
  const found = filesFromArgv(['/usr/bin/electron', '--no-sandbox', '.', '--user-data-dir=/tmp/x', '/m/a.mp3'], {
    defaultApp: true, cwd: '/home/me/app', path: nodePath.posix, kind: kinds({ '/home/me/app': 'directory', '/m/a.mp3': 'file' }),
  });
  assert.deepEqual(found, ['/m/a.mp3']);
  assert.deepEqual(filesFromArgv(['/usr/bin/electron', '--no-sandbox'], { defaultApp: true, cwd: '/', path: nodePath.posix, kind: () => 'directory' }), []);
});

test('switches that Chromium and Electron add are not files', () => {
  const found = filesFromArgv(['/app', '--remote-debugging-port=9222', '--user-data-dir=/tmp/x', '--no-sandbox', '-v', '/m/a.mp3'], {
    path: nodePath.posix, cwd: '/', kind: kinds({ '/m/a.mp3': 'file', '/tmp/x': 'directory' }),
  });
  assert.deepEqual(found, ['/m/a.mp3']);
  // even when a switch happens to name something that exists and looks like a song
  assert.deepEqual(filesFromArgv(['/app', '--song.mp3', '-x.flac'], { path: nodePath.posix, cwd: '/', kind: () => 'file' }), []);
});

test('only audio files that exist, and folders, are taken', () => {
  const table = { '/m/a.MP3': 'file', '/m/notes.txt': 'file', '/m/album': 'directory', '/m/fake.mp3': null, '/m/odd.wav': 'file' };
  const found = filesFromArgv(['/app', '/m/a.MP3', '/m/notes.txt', '/m/album', '/m/fake.mp3', '/m/gone.mp3', '/m/odd.wav'], { path: nodePath.posix, cwd: '/', kind: kinds(table) });
  assert.deepEqual(found, ['/m/a.MP3', '/m/album', '/m/odd.wav'], 'upper-case extensions count; a text file, a missing file are left out');
});

test('repeats are dropped and the order is kept', () => {
  const found = filesFromArgv(['/app', '/m/b.mp3', '/m/a.mp3', '/m/b.mp3', '/m/./b.mp3'], { path: nodePath.posix, cwd: '/', kind: kinds({ '/m/a.mp3': 'file', '/m/b.mp3': 'file' }) });
  assert.deepEqual(found, ['/m/b.mp3', '/m/a.mp3']);
});

test('blank or odd arguments are ignored and no argument list is fine', () => {
  assert.deepEqual(filesFromArgv(['/app', '', '   ', 5, null, undefined], { path: nodePath.posix, cwd: '/', kind: () => 'file' }), []);
  assert.deepEqual(filesFromArgv(undefined), []);
  assert.deepEqual(filesFromArgv([]), []);
  assert.deepEqual(filesFromArgv(['/app']), []);
});

test('a very long list is cut off', () => {
  const names = Array.from({ length: MAX_ARGUMENT_PATHS + 100 }, (_, n) => `/m/${n}.mp3`);
  const found = filesFromArgv(['/app', ...names], { path: nodePath.posix, cwd: '/', kind: () => 'file' });
  assert.equal(found.length, MAX_ARGUMENT_PATHS);
  assert.equal(found[0], '/m/0.mp3');
});

test('with the real file system a real song is found and a folder too', () => {
  const fs = require('node:fs');
  const os = require('node:os');
  const dir = fs.mkdtempSync(nodePath.join(os.tmpdir(), 'mtk-launch-'));
  try {
    const song = nodePath.join(dir, 'one.ogg');
    fs.writeFileSync(song, 'x');
    fs.writeFileSync(nodePath.join(dir, 'two.txt'), 'x');
    assert.deepEqual(filesFromArgv(['app', song, nodePath.join(dir, 'two.txt'), dir], { cwd: '/' }), [song, dir]);
    assert.deepEqual(filesFromArgv(['app', 'one.ogg'], { cwd: dir }), [song], 'relative to the folder the program was started from');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
