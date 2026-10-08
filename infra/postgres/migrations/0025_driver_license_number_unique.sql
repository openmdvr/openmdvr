-- Provisioning guardrail: prevent duplicate drivers within a tenant. Same
-- pattern as vehicles.plate (partial unique index vehicles_tenant_plate_unique,
-- migration 0014).
--
-- The NAME is never the key: two real drivers can share a name. The license
-- number is what uniquely identifies a person, just as the plate identifies a
-- vehicle. Partial (WHERE license_number IS NOT NULL) because the field is
-- optional; two drivers without a number yet must not collide.
-- Case-insensitive and trimmed so data-entry variations cannot create a
-- real duplicate.
CREATE UNIQUE INDEX drivers_tenant_license_number_unique
    ON drivers (tenant_id, lower(btrim(license_number)))
    WHERE license_number IS NOT NULL AND btrim(license_number) <> '';

COMMENT ON INDEX drivers_tenant_license_number_unique IS
    'Prevents two drivers with the same license number (case-insensitive, trimmed) within a tenant. The name is intentionally NOT unique. Same pattern as vehicles_tenant_plate_unique.';
