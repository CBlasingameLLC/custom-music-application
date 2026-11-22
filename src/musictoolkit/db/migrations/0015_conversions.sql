-- Songs converted into another format. What was converted, into which format and folder, and the files that made, so that a conversion can be looked up
-- and taken back. The converted songs are files in a folder the person chose, outside the library: nothing in here is the music itself.
CREATE TABLE conversions (
  id          INTEGER PRIMARY KEY,
  batch_id    TEXT NOT NULL UNIQUE,
  title       TEXT NOT NULL,                     -- what it was called when it began ("Converting 120 songs to MP3")
  format      TEXT NOT NULL,                     -- mp3 | ogg | opus | flac | wav
  quality     TEXT NOT NULL,
  destination TEXT NOT NULL,                     -- the folder the songs went into
  scheme      TEXT NOT NULL,                     -- the layout their names came from
  options     TEXT NOT NULL DEFAULT '{}',        -- the other choices (highest rate, depth, what to do with songs already in the format), as JSON
  status      TEXT NOT NULL DEFAULT 'running'
              CHECK(status IN ('running', 'done', 'partial', 'cancelled', 'failed', 'interrupted')),
  message     TEXT,                              -- why it stopped, when it did not finish
  songs       INTEGER NOT NULL DEFAULT 0,        -- to be written (converted or copied)
  converted   INTEGER NOT NULL DEFAULT 0,
  copied      INTEGER NOT NULL DEFAULT 0,
  failed      INTEGER NOT NULL DEFAULT 0,
  left_out    INTEGER NOT NULL DEFAULT 0,        -- songs not written: left out, already there, a name used twice, or not possible
  bytes       INTEGER NOT NULL DEFAULT 0,        -- written
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  undone_at   TEXT
);
CREATE INDEX idx_conversions_started ON conversions(started_at);

CREATE TABLE conversion_items (
  id            INTEGER PRIMARY KEY,
  conversion_id INTEGER NOT NULL REFERENCES conversions(id) ON DELETE CASCADE,
  track_id      INTEGER,                         -- the song in the library it came from
  title         TEXT NOT NULL,
  source        TEXT NOT NULL,
  target        TEXT NOT NULL,                   -- where the converted file went (the whole path)
  action        TEXT NOT NULL CHECK(action IN ('convert', 'copy')),
  status        TEXT NOT NULL DEFAULT 'waiting'
                CHECK(status IN ('waiting', 'working', 'done', 'failed', 'skipped')),
  message       TEXT,
  size          INTEGER,                         -- of the finished file, and when it was last changed: a file that is different now
  mtime         REAL                             -- was changed by the person, and is left alone when the conversion is taken back
);
CREATE INDEX idx_conversion_items_conversion ON conversion_items(conversion_id);
