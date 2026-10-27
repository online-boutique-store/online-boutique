-- requires: 0002
-- Saved addresses (1..N per customer). The legacy single-row `addresses` table stays in place,
-- read-only for the new service, until a later contract release.

CREATE TABLE customer_addresses (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id                 uuid NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
    label                       text CHECK (label IS NULL OR char_length(label) <= 40),
    line1                       text NOT NULL CHECK (char_length(line1) BETWEEN 1 AND 100),
    line2                       text CHECK (line2 IS NULL OR char_length(line2) <= 100),
    city                        text NOT NULL CHECK (char_length(city) BETWEEN 1 AND 64),
    state                       char(2) NOT NULL CHECK (state ~ '^[A-Z]{2}$'),
    zip                         text NOT NULL CHECK (zip ~ '^\d{5}(-\d{4})?$'),
    country                     char(2) NOT NULL DEFAULT 'US' CHECK (country = 'US'),
    is_po_box                   boolean NOT NULL DEFAULT false,
    is_default                  boolean NOT NULL DEFAULT false,
    address_verification_status text NOT NULL DEFAULT 'unverified',
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX customer_addresses_one_default ON customer_addresses (customer_id) WHERE is_default;
CREATE INDEX customer_addresses_customer_idx ON customer_addresses (customer_id);

-- Backfill from the legacy table. Only rows that already satisfy the new contract are copied;
-- anything else (junk state, bad ZIP, non-US country) stays in `addresses` only, and the
-- customer is asked to complete their profile.
INSERT INTO customer_addresses (customer_id, line1, city, state, zip, country, is_po_box, is_default)
SELECT a.customer_id, btrim(a.street_address), btrim(a.city), upper(btrim(a.state)), btrim(a.zip_code), 'US',
       btrim(a.street_address) ~* '\mP\.?\s?O\.?\s+Box\M|\mPost\s+Office\s+Box\M', true
  FROM addresses a
 WHERE upper(btrim(a.state)) = ANY (ARRAY[
         'AL','AK','AZ','AR','CA','CO','CT','DE','DC','FL','GA','HI','ID','IL','IN','IA','KS','KY','LA','ME',
         'MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI',
         'SC','SD','TN','TX','UT','VT','VA','WA','WV','WI','WY'])
   AND btrim(a.zip_code) ~ '^\d{5}(-\d{4})?$'
   AND upper(btrim(a.country)) IN ('US', 'USA', 'UNITED STATES', 'UNITED STATES OF AMERICA', 'U.S.')
   AND char_length(btrim(a.street_address)) BETWEEN 1 AND 100
   AND char_length(btrim(a.city)) BETWEEN 1 AND 64
   AND NOT EXISTS (SELECT 1 FROM customer_addresses ca WHERE ca.customer_id = a.customer_id);
