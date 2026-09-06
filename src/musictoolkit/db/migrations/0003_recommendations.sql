-- play_history/recommendations: real accumulated application state.
CREATE TABLE play_history (
  id                     INTEGER PRIMARY KEY,
  track_id               INTEGER REFERENCES tracks(id),
  source                 TEXT NOT NULL CHECK(source IN ('spotify_import', 'future_scrobble')),
  played_at_epoch        INTEGER NOT NULL,
  ms_played              INTEGER,
  raw_artist_name        TEXT,
  raw_track_name         TEXT,
  raw_album_name         TEXT,
  spotify_track_uri      TEXT,
  listenbrainz_submitted INTEGER NOT NULL DEFAULT 0,
  import_batch_id        TEXT
);
CREATE UNIQUE INDEX idx_history_dedup ON play_history(source, spotify_track_uri, played_at_epoch);
CREATE INDEX idx_history_submit_pending ON play_history(listenbrainz_submitted);

CREATE TABLE recommendations (
  id                       INTEGER PRIMARY KEY,
  artist_name              TEXT NOT NULL,
  track_name               TEXT,
  musicbrainz_artist_id    TEXT,
  musicbrainz_recording_id TEXT,
  source                   TEXT NOT NULL,
  score                    REAL,
  reason                   TEXT,
  status                   TEXT NOT NULL DEFAULT 'new'
                           CHECK(status IN ('new', 'owned', 'dismissed', 'accepted')),
  date_suggested           TEXT,
  date_reviewed            TEXT,
  notes                    TEXT
);
CREATE UNIQUE INDEX idx_reco_dedup ON recommendations(artist_name, track_name, source);
