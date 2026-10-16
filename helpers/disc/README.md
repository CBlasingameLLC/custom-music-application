# mtk-disc: the helper for CDs and DVDs

Reading the audio of a CD, looking at what is in a drive, opening its tray and writing discs need the drive's own interface, which
Python cannot reach on its own. This small console program (C#, .NET Framework 4.8, already part of Windows 10 and 11) does the hardware part on
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
| `media {drive}` | What the disc in a recorder is, as far as writing goes: `{"writer": true, "writes": ["CD-R", "CD-RW", "DVD+R", ...], "state": "none" \| "blank" \| "written" \| "unusable", "type": "CD-R", "rewritable": false, "blank": true, "protected": false, "capacity_sectors": 359846, "free_sectors": 359846, "can_audio": true, "speeds": [{"speed": 600, "label": "8x"}, ...], "root": "E:\\"}`. `speed` is in sectors a second, as IMAPI counts it; `root` (present when the disc has something on it) is where its files can be read. |
| `burn_audio {drive, tracks: [{path, sectors}], speed}` | Writes a music CD, song by song. Each `path` is a file of raw CD audio (44.1 kHz, 16 bit, stereo, whole sectors of 2352 bytes, at least 300 of them); the two seconds between songs are added by the recorder. Reports progress in sectors of the disc. Blank CD-R or CD-RW only. |
| `burn_data {drive, label, label_iso, files: [{src, dest}], filesystems, speed}` | Writes the files to a data disc and closes it. `dest` is a path inside the disc with `/` between the names; `filesystems` is some of `iso9660`, `joliet`, `udf`. Refused before anything is written if the files do not fit. |
| `build_iso {label, label_iso, files, filesystems, out}` | The same image as an ISO file at `out` (an absolute path ending in `.iso`, in a folder that exists, that is not a file already). Needs no drive. The file is written as `out.part` and renamed when it is whole; a stop or a failure removes it. |
| `erase {drive, full}` | Erases a rewritable disc (CD-RW, DVD-RW). Cannot be stopped once it has begun. |
| `close` | Answers, then exits. The program also exits when its input ends. |

Every refusal is `{"ok": false, "code": ..., "error": "words for the person"}`. The codes: `no_media`, `media_changed`, `not_ready`,
`read_error` (a sector the drive could not read: a scratch, not a failure of the drive), `not_found`, `not_optical`, `busy`,
`unsupported`, `cancelled`, `failed`, and for writing `not_blank`, `write_protected` (or closed), `no_space` and `underrun` (the
recorder ran out of data while writing: the disc is spoiled).

`drives` entries also say `writer` (true if Windows' disc burning component lists the drive as one that writes) and `writes` (the kinds
of disc it can write), and when it knows, `product` and `profiles` (the MMC profile numbers the drive reports, in hex).

A request that takes long may print `{"event":"progress","id":3,"done":1,"total":10,"message":"..."}` lines before its reply, and
`{"cmd":"cancel","target":3}` (read at once, while request 3 is still being worked on) makes it stop with the code `cancelled`. A stop
that arrives before its request has begun stops it when it does; one that arrives after the answer is forgotten.

With `MTK_DISC_TEST=1` in its environment the program also answers `ticks {n}` (n progress lines), `wait {ms}` (a wait that can be
stopped), `crash` and `imapi_probe` (whether each IMAPI component can be made); `scripts/check_disc_helper.py` uses them. They open no
drive.

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

## How it writes

Writing is Windows' own disc burning component, IMAPI 2 (`Imapi.cs`). Nothing is installed with the program and no administrator rights
are needed.

- **Late-bound.** The IMAPI objects are made from their program ids (`IMAPI2.MsftDiscFormat2Data`, `IMAPI2.MsftDiscFormat2TrackAtOnce`,
  `IMAPI2.MsftDiscFormat2Erase`, `IMAPI2FS.MsftFileSystemImage`, ...) and called by name, so nothing is compiled against a type library
  and a wrong guess about a method is an error from that one call. They are made on the program's multi-threaded apartment, so that a
  burn can be stopped from another thread (`CancelAddTrack`, `CancelWrite`).
- **Streams are the system's.** Every file given to IMAPI is a stream made by `SHCreateStreamOnFileEx`; nothing written in this
  program crosses into IMAPI as a COM object. The image of a data disc is read from IMAPI's own result stream.
- **A music CD** is written with the track-at-once format: `PrepareMedia`, one `AddAudioTrack` for every song, `ReleaseMedia` (which
  closes the disc). IMAPI puts two seconds of silence between songs and pads a song shorter than four seconds. It is refused before
  `PrepareMedia` if the disc is not blank (`not_blank`), is write-protected or closed (`write_protected`) or is too small (`no_space`).
- **A data disc** is a file system image (ISO 9660 with names of up to 31 characters, Joliet with the real names, and UDF for the
  bigger discs) made in memory by IMAPI and written with `IDiscFormat2Data`, closed so that nothing can be added later. A disc that is
  too small is refused when the image is made, before anything is written (`IMAPI_E_IMAGE_TOO_BIG`).
- **Progress** comes from IMAPI's events where they can be connected (the connection point of the object is asked which interface it
  sends them through), and a report goes out once a second whatever the events do, so that a long burn is never silent.
- **Stopping** is carried to IMAPI by a thread that looks at the stop request every tenth of a second; the request then ends with
  `cancelled`. A burn that has begun writing is spoiled, which the Python side tells the person. An erase has no way of being stopped.
- **Sleep.** While a disc is written or erased, `SetThreadExecutionState` keeps Windows from sleeping; a computer that suspends in the
  middle of a burn spoils the disc.
- **IMAPI's error numbers** are turned into the codes above (`Imapi.Fail`); a number it does not know is passed on in IMAPI's own words.

What the build machine can prove without a recorder (`scripts/check_disc_helper.py`): that every IMAPI component can be made, and that
an ISO image of files comes out right: it is read back the way a disc reader would (ISO 9660, Joliet, UDF), every file is compared with
the one it was made from, and an image can be stopped and never replaces a file. What only a recorder can prove is the writing itself:
the speeds, the timing, a stop in the middle, and how other players read the disc.

## Not here (yet)

No SCSI pass-through fallback (`READ CD`), no C2 error pointers, no drive read-offset correction, no AccurateRip: reading is checked by
reading twice and comparing (in Python), which catches a drive that returns different data, not one that returns the same wrong data
every time. Writing: no gapless music CDs (the two-second gaps are IMAPI's), no CD-Text, no multi-session discs, and no erasing of
DVD+RW, DVD-RAM or BD-RE (those are overwritten, not erased).

## Licence

Nothing from outside is included: only Windows APIs are called, so there is no third-party notice for this program.
