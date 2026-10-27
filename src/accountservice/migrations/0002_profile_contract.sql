-- requires: 0001
-- Profile contract (v2 accounts). Additive and backward compatible: every new column is
-- nullable or has a constant default, existing rows become profile_version = 1 ("legacy") and
-- are never rejected for missing phone / split name. Version-2 rows must be complete.

ALTER TABLE customers
    ADD COLUMN first_name           text,
    ADD COLUMN last_name            text,
    ADD COLUMN email_display        text,
    ADD COLUMN phone_e164           text,
    ADD COLUMN profile_version      smallint NOT NULL DEFAULT 1,
    ADD COLUMN profile_completed_at timestamptz;

-- Backfill (idempotent: only touches rows that have not been backfilled).
UPDATE customers
   SET email_display = email
 WHERE email_display IS NULL;

UPDATE customers
   SET first_name = split_part(btrim(name), ' ', 1),
       last_name  = NULLIF(btrim(substr(btrim(name), length(split_part(btrim(name), ' ', 1)) + 1)), '')
 WHERE first_name IS NULL AND btrim(name) <> '';

ALTER TABLE customers
    ADD CONSTRAINT customers_phone_e164_fmt
        CHECK (phone_e164 IS NULL OR phone_e164 ~ '^\+1[2-9][0-9]{2}[2-9][0-9]{6}$') NOT VALID,
    ADD CONSTRAINT customers_profile_version_chk
        CHECK (profile_version IN (1, 2)) NOT VALID,
    ADD CONSTRAINT customers_profile_v2_complete
        CHECK (profile_version < 2 OR (first_name IS NOT NULL AND last_name IS NOT NULL
               AND phone_e164 IS NOT NULL AND email_display IS NOT NULL)) NOT VALID;

-- Legacy rows are version 1 and satisfy all three constraints, so validation is a cheap scan.
ALTER TABLE customers VALIDATE CONSTRAINT customers_phone_e164_fmt;
ALTER TABLE customers VALIDATE CONSTRAINT customers_profile_version_chk;
ALTER TABLE customers VALIDATE CONSTRAINT customers_profile_v2_complete;
