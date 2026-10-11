'use strict';

// Songs handed to the app from outside: "Open with" in Explorer, a file dragged onto the program, the command line.
// Pure functions (the file system and the path flavour are passed in) so both Windows and POSIX paths are tested.

const fs = require('node:fs');
const nodePath = require('node:path');

// The formats the player can decode (a browser engine): what the installer registers the app for.
const AUDIO_EXTENSIONS = ['.mp3', '.flac', '.m4a', '.aac', '.ogg', '.oga', '.opus', '.wav'];
const MAX_ARGUMENT_PATHS = 500;

function defaultKind(file) {
  try {
    const stat = fs.statSync(file);
    return stat.isDirectory() ? 'directory' : stat.isFile() ? 'file' : null;
  } catch {
    return null;
  }
}

/**
 * The songs and folders named on a command line, as absolute paths in the order given, without repeats.
 * `argv` is the process's argument list: the program comes first (and, when run as `electron .`, the app folder, which
 * is the first argument that is not a switch); switches (--something, which Chromium and Electron add on their own)
 * are skipped; relative paths are taken from `cwd`.
 */
function filesFromArgv(argv, { defaultApp = false, cwd = process.cwd(), kind = defaultKind, path = nodePath } = {}) {
  let rest = Array.isArray(argv) ? argv.slice(1) : [];
  if (defaultApp) {
    const appFolder = rest.findIndex((arg) => typeof arg === 'string' && !arg.startsWith('-'));
    rest = appFolder === -1 ? [] : rest.slice(appFolder + 1);
  }
  const found = [];
  for (const arg of rest) {
    if (typeof arg !== 'string' || !arg.trim() || arg.startsWith('-')) continue;
    const absolute = path.resolve(cwd, arg);
    const what = kind(absolute);
    if (what === 'directory' || (what === 'file' && AUDIO_EXTENSIONS.includes(path.extname(absolute).toLowerCase()))) found.push(absolute);
  }
  return [...new Set(found)].slice(0, MAX_ARGUMENT_PATHS);
}

module.exports = { AUDIO_EXTENSIONS, MAX_ARGUMENT_PATHS, filesFromArgv };
