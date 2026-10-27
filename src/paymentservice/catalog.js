'use strict';

// Fixed synthetic demo-card catalog. Card numbers exist ONLY in this file and never leave
// paymentservice: callers see catalog ids, brand, last4 and expiry, and charge with opaque tokens.
const crypto = require('crypto');

const EXP_MONTH = 12;
const EXP_YEAR = 2034;

const CATALOG = {
  demo_visa_1111: { brand: 'visa', number: '4111111111111111' },
  demo_visa_4242: { brand: 'visa', number: '4242424242424242' },
  demo_mc_4444: { brand: 'mastercard', number: '5555555555554444' },
  demo_amex_0005: { brand: 'amex', number: '378282246310005' },
  demo_disc_1117: { brand: 'discover', number: '6011111111111117' },
};

// tok_demo_ + 22 url-safe random characters (~132 bits).
const TOKEN_RE = /^tok_demo_[A-Za-z0-9_-]{22}$/;

function entry(catalogId) {
  const c = Object.prototype.hasOwnProperty.call(CATALOG, catalogId) ? CATALOG[catalogId] : null;
  if (!c) return null;
  return { catalog_id: catalogId, brand: c.brand, last4: c.number.slice(-4), exp_month: EXP_MONTH, exp_year: EXP_YEAR };
}

function newToken() {
  return 'tok_demo_' + crypto.randomBytes(17).toString('base64url').slice(0, 22);
}

module.exports = { entry, newToken, TOKEN_RE, ids: Object.keys(CATALOG) };
