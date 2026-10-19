-- 0055_device_media_node.sql
--
-- Which ZLMediaKit media node a device is currently publishing its video on,
-- when the media plane runs as a pool (ZLM_NODES). The device server maintains
-- it automatically from on_publish's mediaServerId -- nothing is assigned by
-- hand. NULL means no fixed node (auto-distributed, e.g. JT1078). It is a plain
-- text node id, not a foreign key: media nodes live in configuration, not in
-- the database.

ALTER TABLE devices ADD COLUMN IF NOT EXISTS media_node_id text;

COMMENT ON COLUMN devices.media_node_id IS
  'Media node this device currently publishes on (matches a ZLM_NODES id). Maintained automatically by the device server from on_publish; NULL = no fixed node.';
