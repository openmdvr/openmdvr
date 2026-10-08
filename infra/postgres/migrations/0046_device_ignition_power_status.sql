-- Ignition (ACC) and external power status reported by the device.
--
-- - JT808 (cameras): go-jt808 already decodes StatusSignDetails.ACC/.Electricity
--   on EVERY 0x0200 position (standard JT/T808-2019 STATUS field, not a vendor
--   extension); jt808server/location.go simply never read it.
-- - GT06 (CY06-2G/JC261/JC400): the "Terminal Information Content" byte
--   travels on alarm frames (0x26/0x16) and heartbeats. The bit layout is the
--   one used across the Concox ecosystem; see
--   gt06server/handlers.go::parseTerminalInfo for the interpretation and its
--   confidence level.
--
-- Same pattern as devices.last_seen_at: two columns per signal (state + when
-- it last changed), directly on devices. They are NEVER overwritten with NULL
-- when a message does not carry the signal. Extensible: a future signal
-- (armed/defense, door open) follows the same two-column pattern.
--
-- No RLS changes: these are new columns of a row already covered by the
-- existing devices_select/devices_update policies (RLS is per row, not per
-- column), same as sim_number/sim_carrier in 0045. Never editable via
-- PATCH /devices/{id}: they are telemetry reported by the device, not
-- configuration (not part of DeviceUpdate, same as last_seen_at).
ALTER TABLE devices
    ADD COLUMN ignition_on BOOLEAN,
    ADD COLUMN ignition_changed_at TIMESTAMPTZ,
    ADD COLUMN power_connected BOOLEAN,
    ADD COLUMN power_changed_at TIMESTAMPTZ;

COMMENT ON COLUMN devices.ignition_on IS
    'Last ignition (ACC) state reported by the device itself. NEVER editable by a human. NULL = signal not reported yet. See jt808server/location.go and gt06server/handlers.go::parseTerminalInfo.';
COMMENT ON COLUMN devices.ignition_changed_at IS
    'When ignition_on last changed value (not updated on every message identical to the known value).';
COMMENT ON COLUMN devices.power_connected IS
    'Last external power state reported by the device itself (true = normal external power). NULL = signal not reported yet.';
COMMENT ON COLUMN devices.power_changed_at IS
    'When power_connected last changed value.';
