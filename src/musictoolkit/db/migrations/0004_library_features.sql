-- Library features for the desktop app: favorites, smart playlists, small
-- key/value state, and indexes for the sort/filter-heavy library views.
ALTER TABLE tracks ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0;

ALTER TABLE playlists ADD COLUMN kind TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE playlists ADD COLUMN rules_json TEXT;
ALTER TABLE playlists ADD COLUMN updated_at TEXT;

CREATE INDEX idx_tracks_genre ON tracks(genre);
CREATE INDEX idx_tracks_year ON tracks(year);
CREATE INDEX idx_tracks_added ON tracks(date_added);
CREATE INDEX idx_tracks_album_artist ON tracks(album_artist, album);
CREATE INDEX idx_history_track ON play_history(track_id);
CREATE INDEX idx_history_played_at ON play_history(played_at_epoch);
CREATE INDEX idx_playlist_tracks_order ON playlist_tracks(playlist_id, position);

CREATE TABLE kv (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
