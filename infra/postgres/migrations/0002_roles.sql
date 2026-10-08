-- Application role used exclusively by the API (FastAPI) and the JT808 server
-- to write telemetry. Deliberately NOT superuser and NOT BYPASSRLS: tenant
-- isolation depends on this role always honoring Row Level Security, with no
-- implicit exception.
--
-- The password is NEVER set in a versioned file. It is set separately
-- (see 0010_set_role_passwords.sh) from an environment variable, both in local
-- development and in production (via the deployment platform's secrets).
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION;
    END IF;
END
$$;

COMMENT ON ROLE app_user IS
    'Only role used by the API and the JT808 server to connect to Postgres. '
    'Never grant BYPASSRLS or SUPERUSER to this role: tenant isolation '
    'depends on it honoring Row Level Security in every circumstance.';
