# mtk-mtp: the helper for phones and players without a drive letter

Phones and many players connect in MTP mode, which has no drive letter and so no folder to copy to. Windows
reaches them through Windows Portable Devices (WPD). This small console program (C#, .NET Framework 4.8, already
part of Windows 10 and 11) does that on behalf of Music Toolkit, on top of the MIT licensed
[MediaDevices](https://github.com/Bassman2/MediaDevices) library (pinned to 1.10.1; 1.10.2 ships a package entry
that cannot be extracted on any file system).

Music Toolkit starts it and talks to it in JSON lines: one request per line on standard input, one reply per line on
standard output. The Python side is `src/musictoolkit/sync/mtp.py`, which describes every command; the first line
the helper prints is `{"event":"ready","version":1}`. Nothing but replies goes to standard output.

```
dotnet build helpers/mtp/MtkMtp.csproj -c Release -o helpers/mtp/out
helpers/mtp/out/mtk-mtp.exe --selftest        # {"ok":true,"helper":"mtk-mtp","version":1,"devices":0}
python scripts/check_mtp_helper.py helpers/mtp/out/mtk-mtp.exe
```

The project also builds on Linux (it references the .NET Framework reference assemblies from NuGet), which is
enough to compile-check it and, under Mono, to exercise the protocol loop; anything that touches a device needs Windows.

## Why it keeps its own folder cache (`Tree.cs`)

The library has no way to ask a device for "the file called x in this folder". To find `\Storage\Music\Artist\Album\x.mp3`
it reads every folder on the way and asks the device about each entry in them, over USB, for every single operation.
With a few hundred artists in `Music` that is seconds per song. The helper reads each folder once per session and
answers `stat`, `list` and folder creation from memory; only `put` and `delete` still go through the library's own path
lookup. A new helper starts for every preview and every sync, so what changed on the phone in between is always seen.

## What it lists

`devices` returns everything Windows Portable Devices lists, including drives that already have a letter (a USB
stick, an SD card, the build machine's own disk), whose storage id is the drive letter (`E:\`). The helper does not
judge that; `phones_only()` in `src/musictoolkit/sync/mtp.py` drops those devices, so that a drive is added as a
drive. A device that lists no storage at all (locked, charging only) is kept and reported with its reason.

## Error codes

Every refusal carries a `code` that Music Toolkit acts on: `no_space` (stop, the device is full), `disconnected`
(check whether the device is still there, stop if not), `not_found`, or `failed` (this file only). The codes are
advisory: the engine checks for itself whether the device is still connected before it gives up.

## Licence

MediaDevices is MIT licensed; its notice is in `THIRD-PARTY-LICENSES.txt`, which is copied next to the dll.
