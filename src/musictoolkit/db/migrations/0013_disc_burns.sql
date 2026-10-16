-- Writing discs. What was burned (or erased, or saved as an ISO file), when, to what kind of disc, and how it ended; nothing in here is the music
-- itself. A burn cannot be taken back, so this is a history and nothing more.
CREATE TABLE disc_burns (
  id          INTEGER PRIMARY KEY,
  batch_id    TEXT NOT NULL UNIQUE,
  kind        TEXT NOT NULL CHECK(kind IN ('audio', 'data', 'erase', 'iso')),
  drive       TEXT,                              -- NULL for an ISO file, which needs no drive
  disc_type   TEXT,                              -- as the recorder said: CD-R, CD-RW, DVD+R ...
  label       TEXT,                              -- the name of a data disc or ISO file
  title       TEXT NOT NULL,                     -- what the history shows: "Road trip", "Erase CD-RW"
  speed       INTEGER,                           -- NULL: the recorder chose
  items       INTEGER NOT NULL DEFAULT 0,        -- songs or files
  sectors     INTEGER,                           -- how much of the disc it took
  status      TEXT NOT NULL DEFAULT 'running'
              CHECK(status IN ('running', 'done', 'cancelled', 'failed', 'interrupted')),
  message     TEXT,                              -- why it stopped, when it did not finish; or a word about the disc afterwards
  spoiled     INTEGER NOT NULL DEFAULT 0,        -- 1: writing had begun when it stopped, so the disc cannot be trusted
  verified    INTEGER,                           -- a data disc read back and compared: 1 matched, 0 did not, NULL not done
  output      TEXT,                              -- the ISO file that was made
  started_at  TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX idx_disc_burns_started ON disc_burns(started_at);

CREATE TABLE disc_burn_items (
  id         INTEGER PRIMARY KEY,
  burn_id    INTEGER NOT NULL REFERENCES disc_burns(id) ON DELETE CASCADE,
  position   INTEGER NOT NULL,                   -- in the order the person made the list (the order on the disc)
  title      TEXT NOT NULL,
  artist     TEXT,
  source     TEXT,                               -- the file it was made from
  track_id   INTEGER,                            -- the song in the library
  sectors    INTEGER,
  status     TEXT NOT NULL DEFAULT 'waiting'
             CHECK(status IN ('waiting', 'done', 'skipped', 'failed')),
  message    TEXT
);
CREATE INDEX idx_disc_burn_items_burn ON disc_burn_items(burn_id);
