-- Library tools: everything that changes the user's files keeps an undo log,
-- and slow work (MusicBrainz lookups) keeps a review queue that survives a restart.

-- One row per file changed by a tag edit; batch_id groups one "Save" for undo.
-- before/after hold the raw easy-tag values ({"title": "Old", "date": null, ...}).
CREATE TABLE tag_edits (
  id          INTEGER PRIMARY KEY,
  batch_id    TEXT NOT NULL,
  track_id    INTEGER NOT NULL,
  before_json TEXT NOT NULL,
  after_json  TEXT NOT NULL,
  edited_at   TEXT NOT NULL
);
CREATE INDEX idx_tag_edits_batch ON tag_edits(batch_id);

-- MusicBrainz matches waiting for a human decision.
CREATE TABLE tag_proposals (
  track_id      INTEGER PRIMARY KEY,
  proposed_json TEXT NOT NULL,
  confidence    REAL NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'applied', 'dismissed')),
  created_at    TEXT NOT NULL,
  resolved_at   TEXT
);
CREATE INDEX idx_tag_proposals_status ON tag_proposals(status, confidence);

-- Files moved by Organize or quarantined as duplicates, so either can be undone.
-- sidecars_json lists [[old, new], ...] companion files (lyrics) moved with the track.
CREATE TABLE file_moves (
  id            INTEGER PRIMARY KEY,
  batch_id      TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK(kind IN ('organize', 'quarantine')),
  track_id      INTEGER,
  old_path      TEXT NOT NULL,
  new_path      TEXT NOT NULL,
  sidecars_json TEXT,
  moved_at      TEXT NOT NULL,
  undone_at     TEXT
);
CREATE INDEX idx_file_moves_batch ON file_moves(batch_id);
