-- Internet radio and podcasts. Like the release radar and the lyrics, everything here is what the app learnt from other services (a
-- directory, a feed) and what the person did with it; nothing is ever written into the music folders, and listening to a station or a
-- podcast never enters the music history, the statistics or the mixes.
CREATE TABLE radio_stations (
  id             INTEGER PRIMARY KEY,
  source         TEXT NOT NULL DEFAULT 'manual',  -- radio-browser (found in the directory) or manual (typed in)
  source_id      TEXT,                            -- the directory's own id for it
  name           TEXT NOT NULL,
  stream_url     TEXT NOT NULL UNIQUE,
  homepage       TEXT,
  art_url        TEXT,
  tags           TEXT,                            -- comma separated
  country        TEXT,
  country_code   TEXT,
  language       TEXT,
  codec          TEXT,
  bitrate        INTEGER,
  favorite       INTEGER NOT NULL DEFAULT 0,
  added_at       TEXT NOT NULL,
  last_played_at TEXT,
  play_count     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_radio_favorite ON radio_stations(favorite, name);
CREATE INDEX idx_radio_recent ON radio_stations(last_played_at);

CREATE TABLE podcasts (
  id              INTEGER PRIMARY KEY,
  feed_url        TEXT NOT NULL UNIQUE,
  title           TEXT NOT NULL,
  author          TEXT,
  description     TEXT,
  site_url        TEXT,
  art_url         TEXT,
  itunes_id       TEXT,
  subscribed_at   TEXT NOT NULL,
  last_checked_at TEXT,                           -- when the feed was last asked about, answered or not
  last_changed_at TEXT,                           -- when it last had something new
  etag            TEXT,
  last_modified   TEXT,
  error           TEXT                            -- why the last check failed, if it did
);

CREATE TABLE podcast_episodes (
  id               INTEGER PRIMARY KEY,
  podcast_id       INTEGER NOT NULL REFERENCES podcasts(id) ON DELETE CASCADE,
  guid             TEXT NOT NULL,
  title            TEXT NOT NULL,
  description      TEXT,
  published_at     TEXT,                          -- ISO 8601, UTC
  duration_seconds REAL,
  audio_url        TEXT NOT NULL,
  audio_type       TEXT,
  audio_bytes      INTEGER,
  art_url          TEXT,
  season           INTEGER,
  episode          INTEGER,
  position_seconds REAL NOT NULL DEFAULT 0,       -- where listening stopped
  played           INTEGER NOT NULL DEFAULT 0,
  played_at        TEXT,
  UNIQUE(podcast_id, guid)
);
CREATE INDEX idx_episodes_podcast ON podcast_episodes(podcast_id, published_at DESC);
CREATE INDEX idx_episodes_new ON podcast_episodes(played, published_at DESC);
