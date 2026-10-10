"""Start the real mtk-mtp.exe and check that it speaks the protocol Music Toolkit expects.

    python scripts/check_mtp_helper.py helpers/mtp/out/mtk-mtp.exe

No phone is plugged into a build machine, so this proves what can be proved without one: the program
starts, says it is ready, answers a refusal as a refusal, and asks Windows Portable Devices what is
plugged in (nothing). Whether copying works on a given phone can only be checked with that phone.

Run it with the Python that has Music Toolkit installed (it uses the app's own client for the helper).
"""

from __future__ import annotations

import errno
import json
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

failures: list[str] = []


def check(ok: bool, message: str) -> None:
    print(("ok    " if ok else "FAIL  ") + message, flush=True)
    if not ok:
        failures.append(message)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    exe = str(Path(sys.argv[1]).resolve())
    if not Path(exe).is_file():
        sys.exit(f"{exe} does not exist")

    from musictoolkit.sync.mtp import MtpHelper, MtpUnavailable
    from musictoolkit.sync.targets import TargetError

    done = subprocess.run([exe, "--selftest"], capture_output=True, text=True, timeout=120)
    print(f"--selftest: exit {done.returncode}, stdout {done.stdout.strip()!r}, stderr {done.stderr.strip()[-400:]!r}", flush=True)
    selftest = {}
    try:
        selftest = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        pass
    check(done.returncode == 0 and selftest.get("ok") is True, "--selftest asks Windows Portable Devices what is plugged in")
    check(selftest.get("version") == 1, "it speaks protocol version 1")
    check(selftest.get("devices") == 0, f"nothing is plugged into this machine ({selftest.get('devices')})")

    try:
        with MtpHelper([exe]) as helper:
            check(True, "it starts and says it is ready")
            devices = helper.call("devices")["devices"]
            check(devices == [], f"the device list is empty ({devices})")

            for cmd, args in (("nonsense", {}), ("stat", {"storage": "s1", "path": "x"}), ("put", {"storage": "s1", "path": "x", "source": "y"})):
                try:
                    helper.call(cmd, **args)
                    check(False, f"{cmd} with no device open is refused")
                except TargetError as error:
                    check(True, f"{cmd} with no device open is refused ({error.strerror})")

            try:
                helper.call("open", device="no-such-device")
                check(False, "opening a device that is not there is refused")
            except TargetError as error:
                check(error.errno == errno.ENOENT, f"opening a device that is not there says so ({error.strerror})")

            check(helper.call("devices")["devices"] == [], "it still answers after refusing things")
    except (MtpUnavailable, TargetError) as error:
        check(False, f"talking to the helper: {error}")

    if failures:
        sys.exit(f"\n{len(failures)} check(s) failed")
    print("\nthe MTP helper is fine", flush=True)


if __name__ == "__main__":
    main()
