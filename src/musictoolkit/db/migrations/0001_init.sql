-- tracks: rebuildable cache of file-derived metadata.
-- Source of truth is the audio files themselves (read via mutagen) — this
-- table can be dropped and rebuilt from a rescan with no data loss.
CREATE TABLE tracks (
  id                       INTEGER PRIMARY KEY,
  file_path                TEXT UNIQUE NOT NULL,
  file_hash                TEXT,
  file_size                INTEGER,
  file_mtime               REAL,
  duration_seconds         REAL,
  title                    TEXT,
  artist                   TEXT,
  album_artist             TEXT,
  album                    TEXT,
  track_number             INTEGER,
  disc_number              INTEGER,
  year                     INTEGER,
  genre                    TEXT,
  musicbrainz_recording_id TEXT,
  musicbrainz_release_id   TEXT,
  musicbrainz_artist_id    TEXT,
  mb_match_confidence      REAL,
  tag_source               TEXT,
  rating                   INTEGER,
  format                   TEXT,
  bitrate                  INTEGER,
  date_added               TEXT,
  date_last_scanned        TEXT,
  is_missing               INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_tracks_artist_album ON tracks(artist, album, title);
CREATE INDEX idx_tracks_mbid ON tracks(musicbrainz_recording_id);
