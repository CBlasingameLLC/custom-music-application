const { spawn } = require('node:child_process');
const path = require('node:path');
const http = require('node:http');
const { app } = require('electron');

const PORT = 4533;
let child = null;

function resolveBackendCommand() {
  if (app.isPackaged) {
    return {
      command: path.join(process.resourcesPath, 'mtk-backend.exe'),
      args: ['dashboard', '--port', String(PORT)],
    };
  }
  // Dev mode: run straight from the developer's own venv so there's no
  // PyInstaller rebuild step on every iteration. Assumes `npm start` is run
  // from this desktop/ directory, with the venv at the repo root (../.venv),
  // matching the documented dev setup.
  const venvPython = path.join(process.cwd(), '..', '.venv', 'Scripts', 'python.exe');
  return { command: venvPython, args: ['-m', 'musictoolkit.cli', 'dashboard', '--port', String(PORT)] };
}

function waitForHttpOk(port, timeoutMs = 15000) {
  // PyInstaller onefile builds self-extract on every launch — observed
  // ~4s cold-start during development, so this default leaves real margin.
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const req = http.get({ host: '127.0.0.1', port, path: '/', timeout: 1000 }, (res) => {
        res.resume();
        resolve();
      });
      req.on('error', () => {
        if (Date.now() > deadline) reject(new Error(`Backend did not respond on port ${port} within ${timeoutMs}ms`));
        else setTimeout(attempt, 300);
      });
      req.on('timeout', () => req.destroy());
    };
    attempt();
  });
}

function startBackend() {
  return new Promise((resolve, reject) => {
    const { command, args } = resolveBackendCommand();
    child = spawn(command, args, { windowsHide: true, stdio: 'ignore' });
    child.once('error', reject);
    waitForHttpOk(PORT).then(() => resolve(PORT)).catch(reject);
  });
}

function stopBackend() {
  if (child && !child.killed) {
    child.kill();
    child = null;
  }
}

module.exports = { startBackend, stopBackend };
