const { spawn, execFileSync } = require('node:child_process');
const path = require('node:path');
const http = require('node:http');
const net = require('node:net');
const fs = require('node:fs');
const os = require('node:os');
const { app } = require('electron');

// Same per-user folder the Python side defaults to (musictoolkit.config.default_data_dir).
const DATA_DIR = path.join(os.homedir(), '.musictoolkit');
const LOG_FILE = path.join(DATA_DIR, 'logs', 'backend.log');

let child = null;
let childExit = null; // set once the backend process has exited or failed to spawn

function resolveBackendCommand(port) {
  const args = ['dashboard', '--port', String(port)];
  if (app.isPackaged) {
    return { command: path.join(process.resourcesPath, 'mtk-backend.exe'), args };
  }
  // Dev mode: run straight from the developer's own venv so there's no
  // PyInstaller rebuild step on every iteration. Assumes `npm start` is run
  // from this desktop/ directory, with the venv at the repo root (../.venv),
  // matching the documented dev setup.
  const venvPython = path.join(process.cwd(), '..', '.venv', 'Scripts', 'python.exe');
  return { command: venvPython, args: ['-m', 'musictoolkit.cli', ...args] };
}

// A fixed port collides with an orphaned backend from a previous run (or any
// other app) — and the health check below would then happily accept the
// stranger's answer. Ask the OS for a free one instead.
function findFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

// PyInstaller onefile builds self-extract on every launch (~4s observed on
// Linux; Windows Defender scanning the fresh files can add a lot more), so the
// default leaves real margin.
function waitForBackend(port, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  let lastProblem = 'no response yet';
  return new Promise((resolve, reject) => {
    const retry = () => {
      if (Date.now() > deadline) {
        reject(new Error(`The backend did not become healthy within ${timeoutMs / 1000}s (${lastProblem}).\nDetails: ${LOG_FILE}`));
      } else {
        setTimeout(attempt, 300);
      }
    };
    const attempt = () => {
      if (childExit) {
        const why = childExit.error ? childExit.error.message : `exit code ${childExit.code}`;
        reject(new Error(`The backend stopped during startup (${why}).\nDetails: ${LOG_FILE}`));
        return;
      }
      const req = http.get({ host: '127.0.0.1', port, path: '/', timeout: 1000 }, (res) => {
        res.resume();
        if (res.statusCode < 500) {
          resolve();
        } else {
          lastProblem = `HTTP ${res.statusCode}`;
          retry();
        }
      });
      req.on('error', (err) => {
        lastProblem = err.code || err.message;
        retry();
      });
      req.on('timeout', () => req.destroy());
    };
    attempt();
  });
}

async function startBackend() {
  fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });
  const port = await findFreePort();
  const { command, args } = resolveBackendCommand(port);

  // stdout/stderr go to a file: uvicorn tracebacks and PyInstaller startup
  // errors are otherwise invisible in a windowless app.
  const logFd = fs.openSync(LOG_FILE, 'a');
  childExit = null;
  child = spawn(command, args, {
    cwd: DATA_DIR,
    windowsHide: true,
    stdio: ['ignore', logFd, logFd],
  });
  fs.closeSync(logFd);

  child.once('error', (error) => {
    childExit = { code: null, error };
  });
  child.once('exit', (code, signal) => {
    childExit = { code, signal };
  });

  await waitForBackend(port);
  return port;
}

function stopBackend() {
  if (!child) return;
  const { pid } = child;
  const alreadyGone = childExit !== null;
  child = null;
  if (alreadyGone) return;
  try {
    if (process.platform === 'win32') {
      // PyInstaller's onefile bootloader launches the real Python process as
      // its own child; killing only the bootloader orphans that process,
      // which keeps running (and keeps its port). /T kills the whole tree.
      execFileSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' });
    } else {
      process.kill(pid);
    }
  } catch {
    // Already exited between the check and the kill.
  }
}

module.exports = { startBackend, stopBackend };
