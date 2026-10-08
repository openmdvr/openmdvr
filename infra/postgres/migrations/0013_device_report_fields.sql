-- Unit data for fleet reports/control. All optional and descriptive (same as
-- vehicle_plate in 0006_devices.sql): the JT808 protocol does not use them;
-- they let operators identify and report on the real vehicle behind each camera.
ALTER TABLE devices
    ADD COLUMN vehicle_make  TEXT,
    ADD COLUMN vehicle_model TEXT,
    ADD COLUMN vehicle_year  SMALLINT CHECK (vehicle_year BETWEEN 1980 AND 2100),
    ADD COLUMN driver_name   TEXT,
    ADD COLUMN notes         TEXT;

COMMENT ON COLUMN devices.vehicle_make IS 'Vehicle make (e.g. "Freightliner"). Descriptive, not used by the protocol.';
COMMENT ON COLUMN devices.vehicle_model IS 'Vehicle model. Descriptive.';
COMMENT ON COLUMN devices.vehicle_year IS 'Vehicle year. Descriptive.';
COMMENT ON COLUMN devices.driver_name IS 'Assigned driver, free text entered by an admin. Not sourced from JT808 (that would be message 0x0702, not implemented).';
COMMENT ON COLUMN devices.notes IS 'Free-form maintenance/installation notes.';
