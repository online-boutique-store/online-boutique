'use strict';

// Durable idempotency for the (mock) payment processor. The "charge" is the durable record: it is
// written to the dedicated order-queue Redis (AOF appendfsync always) BEFORE the call returns, so
//   * repeating a charge with the same idempotency key returns the ORIGINAL transaction id (never a
//     second charge, even across restarts), and
//   * GetCharge can answer "was this key charged?" for a caller that never received the response.
// If Redis is unavailable the charge FAILS (UNAVAILABLE) rather than succeeding without a record.

const { v5: uuidv5 } = require('uuid');
const api = require('@opentelemetry/api');

const TTL_MS = 30 * 24 * 3600 * 1000;
const NAMESPACE = '7a1f3c52-9b84-4e0d-a6c1-2d5e8f0b3a47';

const RECORD = `
local ex = redis.call('HGET', KEYS[1], 'txn')
if ex then redis.call('HINCRBY', KEYS[1], 'calls', 1); return {ex, 1, redis.call('HGET', KEYS[1], 'amount')} end
redis.call('HSET', KEYS[1], 'txn', ARGV[1], 'amount', ARGV[2], 'created_ms', ARGV[3], 'calls', 1)
redis.call('PEXPIRE', KEYS[1], ARGV[4])
return {ARGV[1], 0, ARGV[2]}`;

let client = null;
let connecting = null;

function addr() { return process.env.ORDER_QUEUE_ADDR || ''; }
function enabled() { return addr() !== ''; }

async function conn() {
  if (client && client.isReady) return client;
  if (!connecting) {
    const { createClient } = require('redis');
    const c = createClient({
      url: `redis://${addr()}`,
      disableOfflineQueue: true,                     // fail fast; never queue a charge behind an outage
      socket: { connectTimeout: 2000, reconnectStrategy: (n) => Math.min(n * 100, 2000) },
    });
    c.on('error', () => {});
    connecting = c.connect().then(() => { client = c; return c; }).finally(() => { connecting = null; });
  }
  return connecting;
}

function span(name, fn) {
  const [host, port] = (() => { const a = addr(); const i = a.lastIndexOf(':'); return i > 0 ? [a.slice(0, i), Number(a.slice(i + 1))] : [a, 6379]; })();
  return api.trace.getTracer('paymentservice.redis').startActiveSpan(`redis ${name}`, {
    kind: api.SpanKind.CLIENT,
    attributes: { 'db.system': 'redis', 'db.operation': name, 'server.address': host, 'server.port': port, 'peer.service': host.split('.')[0] },
  }, async (s) => {
    try { return await fn(); } catch (e) { s.setStatus({ code: api.SpanStatusCode.ERROR, message: 'redis error' }); s.setAttribute('error.type', e && e.name ? e.name : 'Error'); throw e; } finally { s.end(); }
  });
}

function deterministicTxn(key) { return uuidv5(key, NAMESPACE); }

async function record(key, amount) {
  const amt = `${amount.currency_code}:${amount.units}.${amount.nanos}`;
  return span('charge.record', async () => {
    const c = await conn();
    const r = await c.eval(RECORD, { keys: [`charge:${key}`], arguments: [deterministicTxn(key), amt, String(Date.now()), String(TTL_MS)] });
    return { txn: r[0], replayed: Number(r[1]) === 1, amount: r[2] };
  });
}

async function lookup(key) {
  return span('charge.get', async () => {
    const c = await conn();
    const v = await c.hGetAll(`charge:${key}`);
    return v && v.txn ? { found: true, txn: v.txn, amount: v.amount } : { found: false };
  });
}

// Demo-card tokens: tok:<sha256(token)> -> catalog id. Only the hash of the token is a Redis key, and
// the value is a catalog id (never a card number). Tokens do not expire: saved methods outlive charges.
function tokenKey(token) { return `tok:${require('crypto').createHash('sha256').update(token).digest('hex')}`; }

async function bindToken(token, catalogId) {
  return span('token.bind', async () => {
    const c = await conn();
    await c.set(tokenKey(token), catalogId);
  });
}

async function resolveToken(token) {
  return span('token.resolve', async () => {
    const c = await conn();
    return (await c.get(tokenKey(token))) || '';
  });
}

function testFault() { return process.env.TELEMETRY_TEST_MODE === '1' ? (process.env.PAYMENT_TEST_FAULT || '') : ''; }

module.exports = { enabled, record, lookup, deterministicTxn, testFault, bindToken, resolveToken };
