-- devices/sync_manifest/playlists: real accumulated application state,
-- not derivable from the files themselves — this is what's worth backing up.
CREATE TABLE devices (
  id                     INTEGER PRIMARY KEY,
  label                  TEXT,
  last_seen_mount_path   TEXT,
  created_at             TEXT
);

CREATE TABLE sync_manifest (
  id                  INTEGER PRIMARY KEY,
  device_id           INTEGER NOT NULL REFERENCES devices(id),
  track_id            INTEGER NOT NULL REFERENCES tracks(id),
  dest_relative_path  TEXT,
  source_file_hash    TEXT,
  source_file_mtime   REAL,
  synced_at           TEXT,
  status              TEXT NOT NULL DEFAULT 'synced'
                       CHECK(status IN ('synced', 'pending', 'stale', 'removed'))
);
CREATE UNIQUE INDEX idx_manifest_device_track ON sync_manifest(device_id, track_id);

CREATE TABLE playlists (
  id         INTEGER PRIMARY KEY,
  name       TEXT,
  source     TEXT,
  created_at TEXT
);

CREATE TABLE playlist_tracks (
  playlist_id INTEGER NOT NULL REFERENCES playlists(id),
  track_id    INTEGER NOT NULL REFERENCES tracks(id),
  position    INTEGER
);
