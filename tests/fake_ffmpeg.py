"""A stand-in for ffmpeg, for the tests of transcode.py: it reads the file it is given as the song (one line that says what to do) and writes what that line says.

    ok N MS      N chunks of 1000 bytes, MS milliseconds apart; each begins with [kbps|seconds|number] so that a test can tell what it was asked for
    crash N      N chunks, then a complaint on the error stream (FAKE_STDERR_HEX, as bytes in hex, when it is set) and an exit with 1
    empty        nothing at all, and an exit with 0
    hang         one chunk, and then nothing for a minute

It adds a line to the file named by FAKE_LOG each time it starts to convert (not when it is only asked what it is), and its process number to FAKE_PIDS."""

import os
import sys
import time


def chunk(kbps: str, offset: str, number: int) -> bytes:
    return f"[{kbps}|{offset}|{number:04d}]".encode().ljust(1000, b".")


def main(argv: list[str]) -> int:
    if "-version" in argv:
        print("ffmpeg version stub-1.0 Copyright (c) nobody")
        return 0
    if "-encoders" in argv:
        print(" A....D aac AAC" if os.environ.get("FAKE_NO_LAME") else " A....D libmp3lame libmp3lame MP3 (codec mp3)")
        return 0
    source = argv[argv.index("-i") + 1]
    kbps = argv[argv.index("-b:a") + 1].rstrip("k")
    offset = argv[argv.index("-ss") + 1] if "-ss" in argv else "0"
    if os.environ.get("FAKE_LOG"):
        with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
            log.write(" ".join(argv) + "\n")
    if os.environ.get("FAKE_PIDS"):
        with open(os.environ["FAKE_PIDS"], "a", encoding="utf-8") as pids:
            pids.write(f"{os.getpid()}\n")
    words = open(source, encoding="utf-8").read().split()
    mode = words[0]
    out = sys.stdout.buffer
    if mode in ("ok", "crash"):
        count, delay = int(words[1]), (int(words[2]) / 1000 if len(words) > 2 else 0.0)
        for number in range(count):
            out.write(chunk(kbps, offset, number))
            out.flush()
            time.sleep(delay)
        if mode == "crash":
            raw = os.environ.get("FAKE_STDERR_HEX")  # (what to complain with, as bytes: a long complaint, one that is not text)
            if raw:
                sys.stderr.buffer.write(bytes.fromhex(raw))
                sys.stderr.buffer.flush()
            else:
                print("Error: boom", file=sys.stderr)
            return 1
        return 0
    if mode == "hang":
        out.write(chunk(kbps, offset, 0))
        out.flush()
        time.sleep(60)
        return 0
    return 0  # "empty"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
