# Third-party notices

Music Toolkit is built from its own code and from the software below. Each is used under its own licence; this file names them, says what
they are used for and where their source is. The installer carries a copy of this file next to the program.

## Audio: reading and writing FLAC and MP3 (from version 0.8.0)

Ripping a CD writes FLAC and MP3 files, and burning an audio CD reads songs of several kinds. All of that is done by
[python-soundfile](https://github.com/bastibe/python-soundfile) (BSD 3-Clause), which loads a build of
[libsndfile](https://github.com/libsndfile/libsndfile) (LGPL 2.1 or later). The build that python-soundfile ships for Windows
([libsndfile-binaries](https://github.com/bastibe/libsndfile-binaries)) contains these libraries, which keep their own licences:

| Library | What it does here | Licence | Source |
|---|---|---|---|
| libsndfile | the audio file interface | LGPL 2.1 or later | https://github.com/libsndfile/libsndfile |
| libFLAC, libogg, libvorbis, libopus | FLAC, Ogg Vorbis and Opus files | BSD 3-Clause (Xiph.Org) | https://xiph.org/downloads/ |
| LAME | writing MP3 files | LGPL 2 or later | https://lame.sourceforge.io/ |
| mpg123 | reading MP3 files | LGPL 2.1 | https://www.mpg123.de/ |

libsndfile, LAME and mpg123 are used as a library file (`libsndfile_x86_64.dll`, in the `_soundfile_data` folder of the program's package), not
compiled into Music Toolkit's own code. Music Toolkit's source is public (this repository), and the program can be built again with another build
of these libraries: see *Development* in the README and `packaging/mtk_backend.spec`. MP3 patents have expired; nothing here needs a licence fee.

## Other software the program is made of

| Package | Used for | Licence |
|---|---|---|
| [mutagen](https://github.com/quodlibet/mutagen) | reading and writing tags | GPL 2 or later |
| [Pillow](https://github.com/python-pillow/Pillow) | pictures (covers, thumbnails) | MIT-CMU (HPND) |
| [musicbrainzngs](https://github.com/alastair/python-musicbrainzngs) | MusicBrainz | BSD 2-Clause |
| [pylast](https://github.com/pylast/pylast) | Last.fm | Apache 2.0 |
| [send2trash](https://github.com/arsenetar/send2trash) | the Recycle Bin | BSD 3-Clause |
| [FastAPI](https://github.com/fastapi/fastapi), [Starlette](https://github.com/encode/starlette), [uvicorn](https://github.com/encode/uvicorn), [pydantic](https://github.com/pydantic/pydantic), [python-multipart](https://github.com/Kludex/python-multipart) | the local web server | MIT, BSD 3-Clause, Apache 2.0 |
| [pandas](https://github.com/pandas-dev/pandas), [NumPy](https://github.com/numpy/numpy) | listening statistics, audio | BSD 3-Clause (NumPy also bundles 0BSD, MIT, Zlib and CC0 parts) |
| [requests](https://github.com/psf/requests), [certifi](https://github.com/certifi/python-certifi), [truststore](https://github.com/sethmlarson/truststore) | internet requests | Apache 2.0, MPL 2.0, MIT |
| [defusedxml](https://github.com/tiran/defusedxml), [typer](https://github.com/fastapi/typer), [psutil](https://github.com/giampaolo/psutil), [tomli-w](https://github.com/hukkin/tomli-w) | feed reading, command line, drives, settings | PSF, MIT, BSD 3-Clause, MIT |
| [Preact](https://github.com/preactjs/preact) and [htm](https://github.com/developit/htm) (in `static/vendor`, with their notice) | the screens | MIT and Apache 2.0 |
| [Electron](https://github.com/electron/electron), [electron-updater](https://github.com/electron-userland/electron-builder) | the desktop window, updates | MIT |
| [MediaDevices](https://github.com/Bassman2/MediaDevices) (in the phone helper, its notice is in `helpers/mtp`) | phones without a drive letter | MIT |

The disc helper (`helpers/disc`) uses only what Windows itself provides, and the Feather icons (MIT, https://feathericons.com) are drawn in
`static/js/icons.js`.

The version of each package that went into a given build is the one `pyproject.toml` allowed at the time; the licence text of each is in its
own repository, linked above.
