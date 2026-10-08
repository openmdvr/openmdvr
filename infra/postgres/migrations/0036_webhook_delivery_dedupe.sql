-- Webhook delivery idempotency. run_webhook_dispatch_listener() (0035) reacts
-- to EVERY LISTEN/NOTIFY message it receives, and the 'notifications' channel
-- is heard by EVERY running API process. With N replicas listening, each one
-- would enqueue its own webhook_deliveries row for the SAME alarm+endpoint and
-- a tenant would receive the event N times. (Observed in practice when two
-- processes shared one database: each alarm produced two deliveries.)
--
-- dedupe_key is free-form TEXT (not an FK to alarms), like event_type: future
-- event types can be added without touching this schema, as long as the
-- dispatcher sets a stable key derived from the source event's id.
ALTER TABLE webhook_deliveries ADD COLUMN dedupe_key TEXT NOT NULL DEFAULT '';
ALTER TABLE webhook_deliveries ALTER COLUMN dedupe_key DROP DEFAULT;

CREATE UNIQUE INDEX webhook_deliveries_endpoint_dedupe_key_idx
    ON webhook_deliveries (webhook_endpoint_id, dedupe_key);

COMMENT ON COLUMN webhook_deliveries.dedupe_key IS 'Idempotency key of the source event, format "<event_type>:<source event id>" (e.g. "device_alarm:<alarm_id>"). Guarantees ONE delivery per (endpoint, event) regardless of how many API processes listen on the same LISTEN/NOTIFY channel, and prevents a future second event type derived from the SAME source id from silently colliding on the ON CONFLICT. The dispatcher inserts with ON CONFLICT (webhook_endpoint_id, dedupe_key) DO NOTHING.';
