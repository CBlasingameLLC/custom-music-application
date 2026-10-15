"""CDs and DVDs: reading audio discs (ripping) and writing discs (burning).

The hardware part is a small Windows helper program, mtk-disc.exe, which this package talks to (see `musictoolkit.discs.helper`);
everything that decides what to do with a disc (how carefully to read it, how to name and tag the files, what to keep so a rip can be
undone) lives here in Python, where it can be tested with a stand-in drive.
"""
