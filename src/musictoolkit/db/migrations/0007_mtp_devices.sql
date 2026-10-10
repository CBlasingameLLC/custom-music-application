-- Phones and players that have no drive letter (MTP). They are found again by serial number, and the music goes
-- under a folder on one of their storages. For these devices last_seen_mount_path holds "mtp:<serial>:<storage>"
-- so the column stays unique per device and is never mistaken for a path on this computer.
ALTER TABLE devices ADD COLUMN kind TEXT NOT NULL DEFAULT 'folder';
ALTER TABLE devices ADD COLUMN mtp_serial TEXT;
ALTER TABLE devices ADD COLUMN mtp_storage TEXT;
ALTER TABLE devices ADD COLUMN mtp_storage_name TEXT;
ALTER TABLE devices ADD COLUMN mtp_base TEXT;
ALTER TABLE devices ADD COLUMN mtp_model TEXT;
