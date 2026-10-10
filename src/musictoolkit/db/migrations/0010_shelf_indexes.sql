-- The songs of one album, found by the name the library shows for it (a missing artist or album reads "Unknown ...").
-- Home picks its album shelves, and an album's page finds its songs, without reading the whole library.
--
-- The names are spelt with IFNULL here, and in queries.ALBUM_ARTIST_FIND / ALBUM_FIND, though the library's grouping
-- queries spell the same thing with COALESCE. That is deliberate: SQLite uses an expression index only for an
-- expression written the same way, so only a lookup of one album uses this index. Grouping the whole library through
-- it (to list albums or artists) reads every song in index order, which was measured to be slower than reading the
-- table once and sorting.
CREATE INDEX idx_tracks_album_key ON tracks(
  (IFNULL(NULLIF(album_artist, ''), IFNULL(NULLIF(artist, ''), 'Unknown Artist'))),
  (IFNULL(NULLIF(album, ''), 'Unknown Album'))
);

-- The songs played most, and played most recently: Home shows the first few of each.
CREATE INDEX idx_track_plays_plays ON track_plays(plays);
CREATE INDEX idx_track_plays_last ON track_plays(last_played);
