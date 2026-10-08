-- Support for GT06 devices (GPS trackers without a camera, Concox GT06 binary
-- protocol and clones) alongside the existing JT808 (camera/MDVR) devices.
-- See jt808-server/internal/gt06server/ for the TCP server that resolves this
-- identifier. insert_gps_position()/insert_alarm() (0009/0018) were already
-- protocol-agnostic and need no changes.

CREATE TYPE device_protocol AS ENUM ('jt808', 'gt06');

ALTER TABLE devices
    ADD COLUMN protocol device_protocol NOT NULL DEFAULT 'jt808';

-- jt808_terminal_id is no longer mandatory: a gt06 device has none.
ALTER TABLE devices
    ALTER COLUMN jt808_terminal_id DROP NOT NULL;

-- GT06 connection identifier (IMEI, 15 digits). Unlike jt808_terminal_id there
-- is no leading-zero gotcha: the IMEI is a direct hex dump of 8 bytes of the
-- login packet, without the zero stripping done by the BCD decoder of the
-- JT808 library (go-jt808). Globally unique for the same reason as
-- jt808_terminal_id: the GT06 server routes by IMEI before knowing the tenant.
ALTER TABLE devices
    ADD COLUMN gt06_imei TEXT CHECK (gt06_imei ~ '^[0-9]{15}$') UNIQUE;

-- Exactly the identifier matching the declared protocol must be set, and the
-- other must be NULL. Avoids ambiguous or "both empty" rows that RLS/the API
-- would have to guess how to treat.
ALTER TABLE devices
    ADD CONSTRAINT devices_protocol_identifier_match CHECK (
        (protocol = 'jt808' AND jt808_terminal_id IS NOT NULL AND gt06_imei IS NULL)
        OR (protocol = 'gt06' AND gt06_imei IS NOT NULL AND jt808_terminal_id IS NULL)
    );

COMMENT ON COLUMN devices.protocol IS 'Hardware connection protocol: jt808 (camera/MDVR) or gt06 (GPS tracker without camera).';
COMMENT ON COLUMN devices.gt06_imei IS 'GT06 tracker IMEI (15 digits). NULL for jt808 devices; see devices_protocol_identifier_match.';
