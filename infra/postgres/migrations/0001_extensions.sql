-- Extensions required by the schema. All use IF NOT EXISTS so migrations can
-- be re-applied without error on an already initialized database.

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS pgcrypto; -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS citext;   -- case-insensitive email
