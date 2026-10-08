-- Orderly, auditable deactivation of users/devices (soft delete: "deactivate,
-- never really delete"). users.status/devices.status already existed (0005/
-- 0006) and are already enforced: deps.py and api_key_auth.py check
-- users.status on EVERY request (real session revocation), and
-- video.py/device_commands.py require devices.status='active' before live
-- video or a remote command. Exposing these columns via the API therefore
-- activates protections that existed but could not be triggered on purpose.
--
-- status_changed_by/status_changed_at (same generic names on both tables
-- rather than "disabled_by"): the same pair records BOTH deactivation and
-- reactivation. Unlike api_keys.revoked_at (monotonic), a user/device status
-- can toggle both ways, so the audit is "who made the LAST status change",
-- not a full history table.
ALTER TABLE users ADD COLUMN status_changed_by UUID NULL REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE users ADD COLUMN status_changed_at TIMESTAMPTZ NULL;

COMMENT ON COLUMN users.status_changed_by IS 'Who made the last status change (activate/deactivate). NULL if it never changed from its default at creation.';
COMMENT ON COLUMN users.status_changed_at IS 'When the last status change happened.';

ALTER TABLE devices ADD COLUMN status_changed_by UUID NULL REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE devices ADD COLUMN status_changed_at TIMESTAMPTZ NULL;

COMMENT ON COLUMN devices.status_changed_by IS 'Who made the last status change (active/inactive/maintenance). NULL if it never changed from its default at creation.';
COMMENT ON COLUMN devices.status_changed_at IS 'When the last status change happened.';
