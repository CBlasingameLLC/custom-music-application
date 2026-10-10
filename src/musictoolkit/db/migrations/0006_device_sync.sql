-- Device sync: remember which physical volume a device is (a drive letter can change between plugs),
-- what was chosen to go on it, when it last synced, and how big each synced copy is (so a copy that was
-- cut short, or replaced on the device, is noticed and copied again).
ALTER TABLE devices ADD COLUMN volume_serial TEXT;
ALTER TABLE devices ADD COLUMN volume_label TEXT;
ALTER TABLE devices ADD COLUMN fs TEXT;
ALTER TABLE devices ADD COLUMN last_synced_at TEXT;
ALTER TABLE devices ADD COLUMN prefs_json TEXT;

ALTER TABLE sync_manifest ADD COLUMN size INTEGER;
