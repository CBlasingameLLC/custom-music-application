-- CDs and DVDs. What was ripped from which disc and the songs it made, so that a rip can be looked up and taken back; nothing in here is
-- the music itself (that is in the music folders and in the tracks table like any other song).
CREATE TABLE disc_rips (
  id          INTEGER PRIMARY KEY,
  batch_id    TEXT NOT NULL UNIQUE,
  disc_id     TEXT,                              -- the MusicBrainz disc ID of the disc
  release_id  TEXT,                              -- the album on MusicBrainz, when it was found there
  album       TEXT NOT NULL,
  artist      TEXT NOT NULL,
  drive       TEXT,
  format      TEXT NOT NULL,                     -- flac | mp3
  quality     TEXT NOT NULL,
  accuracy    TEXT NOT NULL,                     -- verified | fast
  root        TEXT NOT NULL,                     -- the library folder the songs went into
  folder      TEXT,                              -- the album's folder
  status      TEXT NOT NULL DEFAULT 'running'
              CHECK(status IN ('running', 'done', 'partial', 'cancelled', 'failed', 'interrupted')),
  message     TEXT,                              -- why it stopped, when it did not finish
  problems    INTEGER NOT NULL DEFAULT 0,        -- blocks in doubt or unreadable, over all the songs
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  undone_at   TEXT,
  cover_path  TEXT,                              -- the cover.jpg this rip put next to the songs (if it did), and how it was left:
  cover_size  INTEGER,                           -- one that is different now was replaced by the person, and is left alone
  cover_mtime REAL
);
CREATE INDEX idx_disc_rips_started ON disc_rips(started_at);

CREATE TABLE disc_rip_tracks (
  id         INTEGER PRIMARY KEY,
  rip_id     INTEGER NOT NULL REFERENCES disc_rips(id) ON DELETE CASCADE,
  number     INTEGER NOT NULL,                   -- the track's number on the disc
  title      TEXT NOT NULL,
  file_path  TEXT,                               -- where the song goes (set when planned)
  size       INTEGER,                            -- of the finished file, and when it was last changed: a song
  mtime      REAL,                               -- that is different now is left alone when the rip is taken back
  track_id   INTEGER,                            -- the song in the library (NULL until it is added)
  rereads    INTEGER NOT NULL DEFAULT 0,
  suspect    INTEGER NOT NULL DEFAULT 0,         -- blocks whose reads never agreed
  unreadable INTEGER NOT NULL DEFAULT 0,         -- blocks the drive could not read at all
  status     TEXT NOT NULL DEFAULT 'waiting'
             CHECK(status IN ('waiting', 'ripping', 'done', 'problem', 'skipped', 'failed')),
  message    TEXT
);
CREATE INDEX idx_disc_rip_tracks_rip ON disc_rip_tracks(rip_id);
