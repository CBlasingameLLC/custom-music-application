-- Files that are only tags: an MP3 that is just an ID3 tag, an empty file. The scan finds them when it reads a file and marks them here, so that
-- nothing tries to play them (no error for each one in a queue), the screens can say why, and a filter can list them. 0 for every song with sound in it.
ALTER TABLE tracks ADD COLUMN no_audio INTEGER NOT NULL DEFAULT 0;

-- A song whose file never gave a length was read by a version that did not know about this: have the next scan read it again, once.
-- (A file that is only tags gives no length; neither does one that could not be read. The time of the file is what a scan compares.)
UPDATE tracks SET file_mtime = NULL WHERE is_missing = 0 AND duration_seconds IS NULL;
