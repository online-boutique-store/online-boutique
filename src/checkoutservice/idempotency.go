package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/redis/go-redis/v9"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

// Checkout states (the durable state machine; see PlaceOrder). All transitions are Lua scripts that
// check the lease epoch, so a stale worker (paused, partitioned, lease expired) can never advance a
// checkout that a newer worker has taken over.
const (
	stStarted       = "STARTED"
	stPayAttempt    = "PAYMENT_ATTEMPTING"
	stPayCharged    = "PAYMENT_CHARGED"
	stPayDeclined   = "PAYMENT_DECLINED"
	stPayUncertain  = "PAYMENT_UNCERTAIN"
	stShipAttempt   = "SHIPPING_ATTEMPTING"
	stShipped       = "SHIPPED"
	stCompleted     = "COMPLETED"
	stFailedPrePay  = "FAILED_PRE_PAYMENT"
	streamOrders    = "orders:v1"
	streamMaxLen    = 100000
	checkoutTTL     = 30 * 24 * time.Hour
	checkoutKeyPref = "co:"
)

// orderQueue is the dedicated Redis (AOF appendfsync always) that holds checkout state AND the
// orders stream, so a state transition and its stream message are one atomic script.
type orderQueue struct {
	rdb  *redis.Client
	host string
	port int
}

func newOrderQueue() *orderQueue {
	addr := os.Getenv("ORDER_QUEUE_ADDR")
	if addr == "" {
		return nil
	}
	host, port := addr, 6379
	if i := strings.LastIndex(addr, ":"); i > 0 {
		host = addr[:i]
		fmt.Sscanf(addr[i+1:], "%d", &port)
	}
	return &orderQueue{host: host, port: port, rdb: redis.NewClient(&redis.Options{
		Addr: addr, DialTimeout: 2 * time.Second, ReadTimeout: 3 * time.Second, WriteTimeout: 3 * time.Second, MaxRetries: 1})}
}

func (q *orderQueue) span(ctx context.Context, op string) (context.Context, trace.Span) {
	return otel.Tracer("checkoutservice.redis").Start(ctx, "redis "+op, trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(attribute.String("db.system", "redis"), attribute.String("db.operation", op),
			attribute.String("server.address", q.host), attribute.Int("server.port", q.port),
			attribute.String("peer.service", strings.Split(q.host, ".")[0])))
}

var beginScript = redis.NewScript(`
local k = KEYS[1]
local state = redis.call('HGET', k, 'state')
local now = tonumber(ARGV[2])
if not state then
  redis.call('HSET', k, 'fp', ARGV[1], 'state', 'STARTED', 'epoch', 1, 'lease_until', now + tonumber(ARGV[3]),
             'order_id', ARGV[4], 'created_ms', now, 'updated_ms', now)
  redis.call('PEXPIRE', k, ARGV[5])
  return {'NEW', 1, 'STARTED', ''}
end
if redis.call('HGET', k, 'fp') ~= ARGV[1] then return {'CONFLICT', 0, state, ''} end
local epoch = tonumber(redis.call('HGET', k, 'epoch'))
if state == 'COMPLETED' then return {'REPLAY', epoch, state, redis.call('HGET', k, 'resp') or ''} end
if state == 'PAYMENT_DECLINED' or state == 'FAILED_PRE_PAYMENT' then return {'FAILED', epoch, state, redis.call('HGET', k, 'fail_reason') or ''} end
if tonumber(redis.call('HGET', k, 'lease_until')) > now then return {'IN_PROGRESS', epoch, state, ''} end
epoch = epoch + 1
redis.call('HSET', k, 'epoch', epoch, 'lease_until', now + tonumber(ARGV[3]), 'updated_ms', now)
return {'RESUME', epoch, state, ''}
`)

// advance: fenced transition. KEYS[1]=checkout hash, KEYS[2]=stream.
// ARGV: epoch, allowed states (csv), new state, now_ms, lease_ms, publish payload (” = none), phase, order_id,
//
//	traceparent, ttl_ms, then field/value pairs to store.
var advanceScript = redis.NewScript(`
local k = KEYS[1]
if redis.call('HGET', k, 'epoch') ~= ARGV[1] then return {'STALE', ''} end
local state = redis.call('HGET', k, 'state')
local ok = false
for s in string.gmatch(ARGV[2], '[^,]+') do if s == state then ok = true end end
if not ok then return {'BAD_STATE', state} end
local now = tonumber(ARGV[4])
redis.call('HSET', k, 'state', ARGV[3], 'updated_ms', now, 'lease_until', now + tonumber(ARGV[5]))
for i = 11, #ARGV, 2 do redis.call('HSET', k, ARGV[i], ARGV[i+1]) end
local id = ''
if ARGV[6] ~= '' then
  id = redis.call('XADD', KEYS[2], 'MAXLEN', '~', 100000, '*', 'p', ARGV[6], 'phase', ARGV[7], 'order_id', ARGV[8], 'tp', ARGV[9])
  redis.call('HSET', k, 'pub_' .. ARGV[7], id)
end
if ARGV[3] == 'COMPLETED' or ARGV[3] == 'PAYMENT_DECLINED' or ARGV[3] == 'FAILED_PRE_PAYMENT' then redis.call('PEXPIRE', k, ARGV[10]) end
return {'OK', id}
`)

// heartbeat extends the lease of an in-flight checkout, but only while this worker still owns it.
var heartbeatScript = redis.NewScript(`
if redis.call('HGET', KEYS[1], 'epoch') ~= ARGV[1] then return 0 end
local st = redis.call('HGET', KEYS[1], 'state')
if st == 'COMPLETED' or st == 'PAYMENT_DECLINED' or st == 'FAILED_PRE_PAYMENT' then return 0 end
-- release() zeroes lease_until; never resurrect a lease the owner has given up.
if tonumber(redis.call('HGET', KEYS[1], 'lease_until') or '0') == 0 then return 0 end
redis.call('HSET', KEYS[1], 'lease_until', tonumber(ARGV[2]) + tonumber(ARGV[3]))
return 1
`)

// keepLease renews the lease every lease/3 until stop is called, so a slow downstream call does not
// let the janitor take over a checkout that is still being driven.
func (q *orderQueue) keepLease(key string, epoch int64) (stop func()) {
	done := make(chan struct{})
	go func() {
		t := time.NewTicker(checkoutLease / 3)
		defer t.Stop()
		for {
			select {
			case <-done:
				return
			case <-t.C:
				ctx, cancel := context.WithTimeout(context.Background(), time.Second)
				heartbeatScript.Run(ctx, q.rdb, []string{checkoutKeyPref + key}, fmt.Sprint(epoch), nowMs(), checkoutLease.Milliseconds())
				cancel()
			}
		}
	}()
	var once sync.Once
	return func() { once.Do(func() { close(done) }) }
}

type beginResult struct {
	Kind  string // NEW | RESUME | REPLAY | IN_PROGRESS | CONFLICT | FAILED
	Epoch int64
	State string
	Extra string
}

func nowMs() int64 { return time.Now().UnixMilli() }

func (q *orderQueue) begin(ctx context.Context, key, fp, orderID string) (beginResult, error) {
	ctx, sp := q.span(ctx, "checkout.begin")
	defer sp.End()
	res, err := beginScript.Run(ctx, q.rdb, []string{checkoutKeyPref + key}, fp, nowMs(), checkoutLease.Milliseconds(), orderID, checkoutTTL.Milliseconds()).Slice()
	if err != nil {
		sp.RecordError(err)
		sp.SetStatus(codes.Error, "redis error")
		return beginResult{}, err
	}
	return beginResult{Kind: res[0].(string), Epoch: res[1].(int64), State: res[2].(string), Extra: res[3].(string)}, nil
}

var errStale = fmt.Errorf("stale epoch")

// checkoutLease is how long a worker owns a checkout before another may take over (test mode can shorten it).
var checkoutLease = func() time.Duration {
	if os.Getenv("TELEMETRY_TEST_MODE") == "1" {
		if ms, err := strconv.Atoi(os.Getenv("CHECKOUT_LEASE_MS")); err == nil && ms > 0 {
			return time.Duration(ms) * time.Millisecond
		}
	}
	return 45 * time.Second
}()

// advance moves key from one of `from` to `to` under `epoch`, optionally publishing one stream message
// atomically. kv are extra hash fields to store. Returns the stream id ("" when nothing was published).
func (q *orderQueue) advance(ctx context.Context, key string, epoch int64, from []string, to string, publish, phase, orderID string, kv ...string) (string, error) {
	// Capture the caller's span (e.g. orders.publish.completed) before starting the Redis span, so the
	// consumer links to the publish step rather than to the Redis call.
	tp := ""
	if sc := trace.SpanContextFromContext(ctx); sc.IsValid() {
		tp = fmt.Sprintf("00-%s-%s-01", sc.TraceID(), sc.SpanID())
	}
	ctx, sp := q.span(ctx, "checkout.advance")
	defer sp.End()
	args := []interface{}{fmt.Sprint(epoch), strings.Join(from, ","), to, nowMs(), checkoutLease.Milliseconds(), publish, phase, orderID, tp, (24 * 7 * time.Hour).Milliseconds()}
	for _, s := range kv {
		args = append(args, s)
	}
	res, err := advanceScript.Run(ctx, q.rdb, []string{checkoutKeyPref + key, streamOrders}, args...).Slice()
	if err != nil {
		sp.RecordError(err)
		sp.SetStatus(codes.Error, "redis error")
		return "", err
	}
	switch res[0].(string) {
	case "OK":
		return res[1].(string), nil
	case "STALE", "BAD_STATE":
		return "", errStale
	}
	return "", fmt.Errorf("unexpected advance result")
}

func (q *orderQueue) fields(ctx context.Context, key string, names ...string) (map[string]string, error) {
	ctx, sp := q.span(ctx, "checkout.read")
	defer sp.End()
	vals, err := q.rdb.HMGet(ctx, checkoutKeyPref+key, names...).Result()
	if err != nil {
		sp.RecordError(err)
		return nil, err
	}
	out := map[string]string{}
	for i, v := range vals {
		if s, ok := v.(string); ok {
			out[names[i]] = s
		}
	}
	return out, nil
}

func (q *orderQueue) setField(ctx context.Context, key, name, value string) error {
	return q.rdb.HSet(ctx, checkoutKeyPref+key, name, value).Err()
}

// depth returns stream length (diagnostics).
func (q *orderQueue) depth(ctx context.Context) int64 {
	n, _ := q.rdb.XLen(ctx, streamOrders).Result()
	return n
}

// fingerprint binds a key to the request that created it. Card data is only ever HMACed (never stored).
func fingerprint(parts ...string) string {
	key := os.Getenv("CHECKOUT_FP_KEY")
	if key == "" {
		key = os.Getenv("TELEMETRY_REF_KEY")
	}
	m := hmac.New(sha256.New, []byte("fp:"+key))
	for _, p := range parts {
		m.Write([]byte(fmt.Sprintf("%d:%s|", len(p), p)))
	}
	return hex.EncodeToString(m.Sum(nil))
}

// checkoutTestFault returns the test-only crash point (honoured ONLY with TELEMETRY_TEST_MODE=1).
func checkoutTestFault() string {
	if os.Getenv("TELEMETRY_TEST_MODE") != "1" {
		return ""
	}
	return os.Getenv("CHECKOUT_TEST_FAULT")
}

func crashAt(point string) {
	f := checkoutTestFault()
	if f == point {
		os.Exit(137)
	}
	// "sleep_<point>:<ms>" pauses at the point so a test can break a dependency at an exact moment.
	if strings.HasPrefix(f, "sleep_"+point+":") {
		if ms, err := strconv.Atoi(strings.SplitN(f, ":", 2)[1]); err == nil {
			time.Sleep(time.Duration(ms) * time.Millisecond)
		}
	}
}
