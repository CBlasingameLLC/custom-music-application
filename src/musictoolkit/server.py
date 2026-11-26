"""What `mtk serve` decides before it listens: which names it answers to, and the token that signs a device in.

The desktop app listens on 127.0.0.1 with a token made at every launch. A server listens where other devices can reach it, so
it fails closed: it refuses to start on a non-loopback address without the exact names it is reached by, and its token is the
same at every start (a phone that signed in stays signed in) and is never printed in a log.
"""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote

from musictoolkit import backup
from musictoolkit.web.hosts import clean_names
from musictoolkit.web.tailnet import clean_logins

LOOPBACK_BINDS = frozenset({"127.0.0.1", "::1", "localhost"})
MIN_TOKEN_LENGTH = 20  # `openssl rand -base64 32` is 44 characters, token_urlsafe(32) is 43
TOKEN_FILE = "server.token"


class ServeError(Exception):
    """Something that has to be put right before the server can start. The message says what, in words."""


def is_loopback_bind(host: str) -> bool:
    return host.strip().lower() in LOOPBACK_BINDS


def allowed_names(bind: str, allowed_hosts: list[str]) -> list[str]:
    """The names the server answers to besides the loopback ones, checked: a plain host name or address each, and at least one
    when it listens beyond this machine (otherwise every request from another device would be refused, and the person would
    not know why)."""
    try:
        names = clean_names(allowed_hosts)
    except ValueError as error:
        raise ServeError(f"[server] allowed_hosts: {error}.") from None
    if not is_loopback_bind(bind) and not names:
        raise ServeError(
            f"it would listen on {bind}, where other devices can reach it, but [server] allowed_hosts is empty. List the names "
            'devices reach it by (for example allowed_hosts = ["musicpi.local"]); a request that names anything else is refused.'
        )
    return names


def backup_settings(database: object) -> backup.Settings:
    """The settings for copies of the database, checked: the server does not start with ones that would leave it without the copies the person asked for."""
    try:
        return backup.settings_of(database)
    except backup.BackupError as error:
        raise ServeError(str(error)) from None


def tailscale_logins(bind: str, entries: object) -> list[str]:
    """The Tailscale logins that are signed in without the token, checked. Only a server that listens on this computer alone may do that:
    Tailscale Serve, which says whose request it is, runs here and passes the request on, and on any other address a program on the
    network could write the same header and be anyone."""
    if not entries:
        return []
    if isinstance(entries, str) or not isinstance(entries, (list, tuple)):
        raise ServeError('[server] tailscale_logins must be a list of logins in quotes, for example tailscale_logins = ["you@example.com"].')
    try:
        logins = clean_logins(entries)
    except ValueError as error:
        raise ServeError(f"[server] tailscale_logins: {error}.") from None
    if not is_loopback_bind(bind):
        raise ServeError(
            f'[server] tailscale_logins needs host = "127.0.0.1" (it would listen on {bind}): the login comes in a header that Tailscale Serve '
            "writes on this computer, and on any other address a program on the network could write it and be anyone. Empty the list, or "
            "listen on this computer only and reach the server through `tailscale serve`."
        )
    return logins


def check_port(port: int) -> int:
    if not 1 <= int(port) <= 65535:
        raise ServeError(f"{port} is not a port number (1 to 65535).")
    return int(port)


def token_path(data_dir: Path) -> Path:
    return Path(data_dir) / TOKEN_FILE


def server_token(data_dir: Path, environ: Mapping[str, str], *, create: bool = True) -> tuple[str, str]:
    """(the token, where it came from). The MTK_TOKEN environment variable wins (a service manager keeps it in a file only root can
    read); else the token file in the data folder, made at the first start with a random token and readable by its owner only.
    With `create` False a missing file is an error instead of being made: whoever asks for the token (perhaps another user than the
    one the server runs as) must not leave a file the server then cannot read."""
    given = (environ.get("MTK_TOKEN") or "").strip()
    if given:
        if len(given) < MIN_TOKEN_LENGTH:
            raise ServeError(f"MTK_TOKEN is too short to be a secret: use at least {MIN_TOKEN_LENGTH} characters (openssl rand -hex 32).")
        return given, "the MTK_TOKEN environment variable"
    path = token_path(data_dir)
    if path.exists():
        saved = path.read_text(encoding="utf-8").strip()
        if len(saved) < MIN_TOKEN_LENGTH:
            raise ServeError(f"{path} is empty or too short to be a secret. Delete it and a new token is made at the next start.")
        return saved, str(path)
    if not create:
        raise ServeError(f"there is no token yet ({path} does not exist). Start the server once and it makes one, or set MTK_TOKEN.")
    path.parent.mkdir(parents=True, exist_ok=True)
    made = secrets.token_urlsafe(32)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # (another first start made it a moment ago: two starts must end up with the same token, the first one's)
        return server_token(data_dir, environ, create=False)
    with os.fdopen(descriptor, "w", encoding="utf-8") as out:
        out.write(made + "\n")
    return made, str(path)


def token_file_warning(data_dir: Path) -> str | None:
    """A sentence when the token file can be read by someone besides its owner (on a system that has such permissions)."""
    path = token_path(data_dir)
    try:
        mode = path.stat().st_mode
    except OSError:
        return None
    if os.name != "nt" and mode & (stat.S_IRWXG | stat.S_IRWXO):
        return f"{path} can be read by other users of this computer; chmod 600 it."
    return None


def sign_in_link(name: str, token: str) -> str:
    """The address that signs a device in: open it once on each device. `name` is how that device reaches the server
    (https://musicpi.example.ts.net, or http://musicpi.local:4533)."""
    base = name.strip().rstrip("/")
    if "://" not in base:
        raise ServeError(f"{name!r} must start with http:// or https://")
    return f"{base}/?token={quote(token, safe='')}"  # (a token chosen by hand may have characters, + / =, that mean something in an address)
