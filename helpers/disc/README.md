# mtk-disc: the helper for CDs and DVDs

Reading the audio of a CD, looking at what is in a drive and opening its tray need the drive's own interface, which Python cannot
reach on its own. This small console program (C#, .NET Framework 4.8, already part of Windows 10 and 11) does the hardware part on
behalf of Music Toolkit. It uses nothing but Windows itself: no libraries, no drivers, no administrator rights. Everything that is a
decision (checking a read, reading again, encoding, tagging, where the files go) is done in Python
(`src/musictoolkit/discs/`), where it can be tested without a drive.

Music Toolkit starts it and talks to it in JSON lines: one request per line on standard input, one reply per line on standard
output. The Python side is `src/musictoolkit/discs/helper.py`; the first line the helper prints is `{"event":"ready","version":1}`.
Nothing but replies and progress lines goes to standard output.

```
dotnet build helpers/disc/MtkDisc.csproj -c Release -o helpers/disc/out
helpers/disc/out/mtk-disc.exe --selftest        # {"ok":true,"helper":"mtk-disc","version":1,"drives":0,"request_size":16}
python scripts/check_disc_helper.py helpers/disc/out/mtk-disc.exe
```

The project also builds on Linux (it references the .NET Framework reference assemblies from NuGet), which is enough to compile-check
it and, under Mono, to exercise the protocol loop (`scripts/check_disc_helper.py` runs that way too); anything that opens a drive needs
Windows.

## Commands

| Request | Answer |
| --- | --- |
| `drives` | `{"drives": [{"id": "E:", "name": "HL-DT-ST DVDRAM GH24NSD1", "media": "audio", "label": "Audio CD"}]}`. `media` is `none`, `loading` (spinning up), `audio`, `data`, `mixed` (music and a data track, an Enhanced CD) or `unknown` (a blank disc, or one the drive cannot read). |
| `toc {drive}` | `{"first": 1, "last": 12, "leadout": 258575, "tracks": [{"number": 1, "audio": true, "start": 0}, ...]}`. Sectors count from the start of the first track (what Windows calls the logical block); MusicBrainz counts 150 further on, which the Python side adds. |
| `read {drive, lba, count}` | `{"sectors": 16, "data": "<base64 of 2352 bytes a sector>"}`, at most 16 sectors. |
| `flush {drive, lba, count}` | Reads up to 4096 sectors and throws them away, to push what the drive remembers of an earlier read out of its own memory. Sectors that cannot be read do not matter and are passed over. |
| `eject {drive}` | Opens the tray. |
| `close` | Answers, then exits. The program also exits when its input ends. |

Every refusal is `{"ok": false, "code": ..., "error": "words for the person"}`. The codes: `no_media`, `media_changed`, `not_ready`,
`read_error` (a sector the drive could not read: a scratch, not a failure of the drive), `not_found`, `not_optical`, `busy`,
`unsupported`, `cancelled`, `failed`.

A request that takes long may print `{"event":"progress","id":3,"done":1,"total":10,"message":"..."}` lines before its reply, and
`{"cmd":"cancel","target":3}` (read at once, while request 3 is still being worked on) makes it stop with the code `cancelled`. A stop
that arrives before its request has begun stops it when it does; one that arrives after the answer is forgotten.

With `MTK_DISC_TEST=1` in its environment the program also answers `ticks {n}` (n progress lines), `wait {ms}` (a wait that can be
stopped) and `crash`; `scripts/check_disc_helper.py` uses them. They open no drive.

## What it opens

A drive id is exactly one letter and a colon. Anything else (a path, a device name, `\\.\PhysicalDrive0`) is refused as not a drive
letter before anything is opened, and a letter is only opened if `DriveInfo` says it is a CD/DVD drive (a hard disk or a USB stick is
refused as `not_optical`). The page in Music Toolkit never names a device; the Python side checks the id against this program's own
listing as well.

## How it reads

- **Contents** (`IOCTL_CDROM_READ_TOC`): 4 header bytes, then 8 bytes for every track and for the lead-out (track number `0xAA`). The
  address bytes are minute, second, frame; `(minute * 60 + second) * 75 + frame - 150` is the sector. Bit 2 of the control nibble
  marks a data track.
- **Audio** (`IOCTL_CDROM_RAW_READ`, track mode `CDDA`): the input is 16 bytes (`DiskOffset` = sector * 2048, `SectorCount`,
  `TrackMode`), the output is 2352 bytes a sector. This works for an ordinary user.
- **Tray** (`IOCTL_STORAGE_EJECT_MEDIA`, after allowing removal), with the multimedia API (`set cdaudio door open`) as the fallback
  for the drives that refuse the device control.
- The control codes are `CTL_CODE(device, function, method, access) = device << 16 | access << 14 | function << 2 | method`:
  `IOCTL_CDROM_READ_TOC` = `2 << 16 | 1 << 14 | 0x000 << 2 | 0` = `0x24000`; `IOCTL_CDROM_RAW_READ` = `2 << 16 | 1 << 14 | 0x00F << 2 | 2`
  = `0x2403E`; `IOCTL_STORAGE_CHECK_VERIFY` = `0x2D << 16 | 1 << 14 | 0x200 << 2` = `0x2D4800`; `IOCTL_STORAGE_MEDIA_REMOVAL` = `0x2D4804`;
  `IOCTL_STORAGE_EJECT_MEDIA` = `0x2D4808`; `IOCTL_STORAGE_QUERY_PROPERTY` = `0x2D << 16 | 0 << 14 | 0x500 << 2` = `0x2D1400`.

What it does about the drives that do not behave:

- A drive that is still spinning up answers "not ready": the first read waits for it, a quarter of a second at a time.
- Some drives and USB adapters take fewer sectors at once than asked for: a refused request is split in halves and the size that
  worked is kept for the rest of the session.
- Windows reports "media changed" once after a disc changes, and sometimes only because something looked at the drive. A read that gets
  it asks for the contents again; if they are the same, the read goes on, and if not it is `media_changed` and the rip stops.
- The handle to a drive stays open between the requests of a rip, so that a disc taken out in the middle is noticed.

## Not here (yet)

No SCSI pass-through fallback (`READ CD`), no C2 error pointers, no drive read-offset correction, no AccurateRip: reading is checked by
reading twice and comparing (in Python), which catches a drive that returns different data, not one that returns the same wrong data
every time. Burning (IMAPI) comes with the burn commands.

## Licence

Nothing from outside is included: only Windows APIs are called, so there is no third-party notice for this program.
