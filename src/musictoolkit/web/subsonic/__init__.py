"""A Subsonic / OpenSubsonic server API (under /rest) over the library, so that the apps made for that protocol (on an iPhone: Amperfy, play:Sub, Substreamer,
Narjo; on a Mac: Supersonic, Feishin) can use this server. See dispatch.py for how a call is answered, and render.py for what an answer looks like."""

from musictoolkit.web.subsonic import annotate, browse, media, playlists, system  # noqa: F401  (each of these registers the calls it answers)
from musictoolkit.web.subsonic.dispatch import router

__all__ = ["router"]
