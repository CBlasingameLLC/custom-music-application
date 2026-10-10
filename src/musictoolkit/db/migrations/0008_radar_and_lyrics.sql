-- The release radar (new releases by artists you play) and lyrics found online. Both are answers from other
-- services that the app keeps for itself: nothing here is ever written into the music folders.
CREATE TABLE fresh_releases (
  id                 INTEGER PRIMARY KEY,
  release_group_mbid TEXT NOT NULL UNIQUE,
  artist             TEXT NOT NULL,
  artist_mbid        TEXT,
  title              TEXT NOT NULL,
  kind               TEXT,                       -- Album, EP or Single
  release_date       TEXT NOT NULL,              -- YYYY-MM-DD, or less when only the month or the year is known
  source             TEXT NOT NULL,              -- listenbrainz or musicbrainz
  art_url            TEXT,                       -- where the cover can be shown from, when the service says there is one
  plays              INTEGER NOT NULL DEFAULT 0, -- how often this artist was played here, for ordering
  status             TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new', 'dismissed')),
  first_seen         TEXT NOT NULL
);
CREATE INDEX idx_fresh_releases_date ON fresh_releases(release_date);

CREATE TABLE lyrics_cache (
  track_id     INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
  query_key    TEXT NOT NULL,                    -- what was asked (artist, title, album, length): a retagged song asks again
  status       TEXT NOT NULL CHECK(status IN ('found', 'none')),
  synced       TEXT,                             -- JSON [{t, text}]
  plain        TEXT,
  instrumental INTEGER NOT NULL DEFAULT 0,
  source_id    INTEGER,                          -- the id LRCLIB gave these lyrics
  fetched_at   INTEGER NOT NULL
);
