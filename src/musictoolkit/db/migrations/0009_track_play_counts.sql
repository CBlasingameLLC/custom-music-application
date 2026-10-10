-- How many times each song was played and when last: kept up to date by triggers on play_history, so a screen that
-- shows plays or last-played next to every song reads one row per song instead of counting the whole listening history
-- (a long history made every library screen wait for that count). The counts come from play_history only; this table
-- holds nothing that cannot be recomputed from it.
CREATE TABLE track_plays (
  track_id    INTEGER PRIMARY KEY,
  plays       INTEGER NOT NULL,
  last_played INTEGER
);

INSERT INTO track_plays (track_id, plays, last_played)
SELECT track_id, COUNT(*), MAX(played_at_epoch) FROM play_history WHERE track_id IS NOT NULL GROUP BY track_id;

CREATE TRIGGER play_history_counted AFTER INSERT ON play_history WHEN NEW.track_id IS NOT NULL
BEGIN
  INSERT INTO track_plays (track_id, plays, last_played) VALUES (NEW.track_id, 1, NEW.played_at_epoch)
  ON CONFLICT(track_id) DO UPDATE SET plays = plays + 1, last_played = MAX(COALESCE(last_played, 0), NEW.played_at_epoch);
END;

CREATE TRIGGER play_history_uncounted AFTER DELETE ON play_history WHEN OLD.track_id IS NOT NULL
BEGIN
  UPDATE track_plays SET plays = plays - 1,
         last_played = (SELECT MAX(played_at_epoch) FROM play_history WHERE track_id = OLD.track_id)
  WHERE track_id = OLD.track_id;
  DELETE FROM track_plays WHERE track_id = OLD.track_id AND plays <= 0;
END;

-- A play moved from one song to another (a history import matched to the library, duplicates merged, a song forgotten).
CREATE TRIGGER play_history_moved_from AFTER UPDATE OF track_id ON play_history WHEN OLD.track_id IS NOT NULL AND OLD.track_id IS NOT NEW.track_id
BEGIN
  UPDATE track_plays SET plays = plays - 1,
         last_played = (SELECT MAX(played_at_epoch) FROM play_history WHERE track_id = OLD.track_id)
  WHERE track_id = OLD.track_id;
  DELETE FROM track_plays WHERE track_id = OLD.track_id AND plays <= 0;
END;

CREATE TRIGGER play_history_moved_to AFTER UPDATE OF track_id ON play_history WHEN NEW.track_id IS NOT NULL AND OLD.track_id IS NOT NEW.track_id
BEGIN
  INSERT INTO track_plays (track_id, plays, last_played) VALUES (NEW.track_id, 1, NEW.played_at_epoch)
  ON CONFLICT(track_id) DO UPDATE SET plays = plays + 1, last_played = MAX(COALESCE(last_played, 0), NEW.played_at_epoch);
END;

-- Which artists were heard before a date (the Stats screen's "new artists") without reading every earlier play.
CREATE INDEX idx_history_artist ON play_history(LOWER(TRIM(raw_artist_name)), played_at_epoch);

-- A song's plays in time order: the last play of a song after one was removed, and the plays a radio looks at, are
-- found without reading all of that song's plays. It also serves every lookup by song that idx_history_track did.
CREATE INDEX idx_history_track_time ON play_history(track_id, played_at_epoch);
DROP INDEX idx_history_track;
