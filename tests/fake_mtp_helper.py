"""A stand-in for mtk-mtp.exe that speaks the same JSON lines but keeps its "devices" in plain folders.

    python tests/fake_mtp_helper.py <root>

<root>/<serial>/device.json     {"name": "...", "manufacturer": "...", "model": "...", "storages": [{"id": "s1", "name": "Internal storage", "capacity": 1000000}]}
                                ("error": "The device is locked" makes it show up but offer no storage)
<root>/<serial>/<storage id>/   the storage's files
<root>/control.json             optional, read before every command, to make things go wrong:
                                {"unplugged": ["serial"], "noise": true, "free_unknown": true,
                                 "fail_put": {"match": "Song B", "code": "no_space", "partial": true}}
                                (the command "hang" never answers)

Real devices write straight to the final name (there is no rename on MTP), and so does this: a failing put with
"partial" leaves half a file behind, as an unplugged phone would.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path


def folders(path: Path):
    return sorted(p for p in path.iterdir() if p.is_dir()) if path.is_dir() else []


class Fake:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.device: Path | None = None

    def control(self) -> dict:
        try:
            return json.loads((self.root / "control.json").read_text())
        except (OSError, ValueError):
            return {}

    def unplugged(self, serial: str) -> bool:
        return serial in self.control().get("unplugged", [])

    def describe(self, folder: Path, hide_free: bool = True) -> dict:
        info = json.loads((folder / "device.json").read_text())
        storages = []
        for s in info.get("storages", []):
            used = sum(f.stat().st_size for f in (folder / s["id"]).rglob("*") if f.is_file()) if (folder / s["id"]).is_dir() else 0
            storages.append({"id": s["id"], "name": s["name"], "capacity": s["capacity"], "free": max(0, s["capacity"] - used)})
        if hide_free and self.control().get("free_unknown"):
            storages = [{**s, "free": None} for s in storages]  # a phone that does not say how much room it has
        described = {"id": f"fake:{folder.name}", "serial": folder.name, "name": info.get("name", folder.name), "manufacturer": info.get("manufacturer", ""),
                     "model": info.get("model", ""), "storages": storages}
        if info.get("error"):  # found, but it will not open (locked, charging only)
            described.update(storages=[], error=info["error"])
        return described

    def where(self, storage: str, path: str) -> Path:
        assert self.device is not None
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        if ".." in parts:
            raise ValueError("path leaves the storage")
        return self.device.joinpath(storage, *parts)

    def handle(self, request: dict) -> dict:
        cmd = request.get("cmd")
        if cmd == "hang":
            time.sleep(60)
        if self.control().get("noise"):
            print("stray log line from a driver", flush=True)
        if cmd == "devices":
            return {"ok": True, "devices": [self.describe(f) for f in folders(self.root) if (f / "device.json").exists() and not self.unplugged(f.name)]}
        if cmd == "open":
            wanted = str(request.get("device", "")).removeprefix("fake:")
            folder = self.root / wanted
            if not (folder / "device.json").exists() or self.unplugged(wanted):
                return {"ok": False, "code": "not_found", "error": "That device is not connected"}
            self.device = folder
            return {"ok": True, "device": self.describe(folder)}
        if self.device is None:
            return {"ok": False, "code": "failed", "error": "open a device first"}
        if self.unplugged(self.device.name):
            return {"ok": False, "code": "disconnected", "error": "The device was disconnected"}
        storage = request.get("storage", "")
        if cmd == "free":
            info = next(s for s in self.describe(self.device)["storages"] if s["id"] == storage)
            return {"ok": True, "free": info["free"], "capacity": info["capacity"]}
        target = self.where(storage, request.get("path", ""))
        if cmd == "stat":
            return {"ok": True, "exists": target.exists(), "is_dir": target.is_dir(), "size": target.stat().st_size if target.is_file() else None}
        if cmd == "list":
            if not target.is_dir():
                return {"ok": False, "code": "not_found", "error": "No such folder"}
            return {"ok": True, "entries": [{"name": e.name, "is_dir": e.is_dir(), "size": e.stat().st_size if e.is_file() else None} for e in sorted(target.iterdir())]}
        if cmd == "mkdir":
            target.mkdir(parents=True, exist_ok=True)
            return {"ok": True}
        if cmd == "delete":
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            return {"ok": True}
        if cmd == "put":
            source = Path(request["source"])
            size = source.stat().st_size
            failure = self.control().get("fail_put")
            free = next(s for s in self.describe(self.device, hide_free=False)["storages"] if s["id"] == storage)["free"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                free += target.stat().st_size
            if failure and failure.get("match", "") in str(target):
                if failure.get("partial"):
                    target.write_bytes(source.read_bytes()[: size // 2])
                return {"ok": False, "code": failure.get("code", "failed"), "error": "The device stopped accepting the file"}
            if size > free:
                return {"ok": False, "code": "no_space", "error": "There is not enough room on the device"}
            shutil.copyfile(source, target)
            return {"ok": True, "size": target.stat().st_size}
        return {"ok": False, "code": "failed", "error": f"unknown command {cmd!r}"}


def main() -> None:
    fake = Fake(Path(sys.argv[1]))
    print(json.dumps({"event": "ready", "version": 1}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
        if request.get("cmd") == "close":
            print(json.dumps({"id": request.get("id"), "ok": True}), flush=True)
            break
        try:
            reply = fake.handle(request)
        except Exception as exc:  # the real helper also turns any slip into an error reply
            reply = {"ok": False, "code": "failed", "error": f"{type(exc).__name__}: {exc}"}
        reply["id"] = request.get("id")
        print(json.dumps(reply), flush=True)


if __name__ == "__main__":
    main()
