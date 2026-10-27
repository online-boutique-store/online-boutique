package main

import (
	"context"
	"crypto/sha256"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

// shipmentStore makes ShipOrder idempotent. Records live in the dedicated order-queue Redis (AOF,
// appendfsync always) so a replay after a crash/restart returns the ORIGINAL tracking id and never
// creates a second shipment. Without ORDER_QUEUE_ADDR the service behaves as before.
type shipmentStore struct {
	rdb  *redis.Client
	host string
	port int
}

const shipmentTTL = 30 * 24 * time.Hour

var recordShipment = redis.NewScript(`
local existing = redis.call('HGET', KEYS[1], 'tracking_id')
if existing then return {existing, 1} end
redis.call('HSET', KEYS[1], 'tracking_id', ARGV[1], 'created_ms', ARGV[2])
redis.call('PEXPIRE', KEYS[1], ARGV[3])
return {ARGV[1], 0}`)

func newShipmentStore() *shipmentStore {
	addr := os.Getenv("ORDER_QUEUE_ADDR")
	if addr == "" {
		return nil
	}
	host, port := addr, 6379
	if i := strings.LastIndex(addr, ":"); i > 0 {
		host = addr[:i]
		fmt.Sscanf(addr[i+1:], "%d", &port)
	}
	return &shipmentStore{host: host, port: port, rdb: redis.NewClient(&redis.Options{
		Addr: addr, DialTimeout: 2 * time.Second, ReadTimeout: 2 * time.Second, WriteTimeout: 2 * time.Second, MaxRetries: 1})}
}

// redisSpan wraps one Redis operation in a CLIENT span with the peer attributes APM draws a DB edge from.
// Command arguments are never recorded.
func (s *shipmentStore) redisSpan(ctx context.Context, op string) (context.Context, trace.Span) {
	return otel.Tracer("shippingservice.redis").Start(ctx, "redis "+op, trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(attribute.String("db.system", "redis"), attribute.String("db.operation", op),
			attribute.String("server.address", s.host), attribute.Int("server.port", s.port), attribute.String("peer.service", strings.Split(s.host, ".")[0])))
}

// record stores the tracking id for key the first time and returns (id, replayed).
func (s *shipmentStore) record(ctx context.Context, key, candidate string) (string, bool, error) {
	ctx, span := s.redisSpan(ctx, "shipment.record")
	defer span.End()
	res, err := recordShipment.Run(ctx, s.rdb, []string{"ship:" + key}, candidate, time.Now().UnixMilli(), shipmentTTL.Milliseconds()).Slice()
	if err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, "redis error")
		return "", false, err
	}
	return res[0].(string), res[1].(int64) == 1, nil
}

func (s *shipmentStore) get(ctx context.Context, key string) (string, bool, error) {
	ctx, span := s.redisSpan(ctx, "shipment.get")
	defer span.End()
	v, err := s.rdb.HGet(ctx, "ship:"+key, "tracking_id").Result()
	if err == redis.Nil {
		return "", false, nil
	}
	if err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, "redis error")
		return "", false, err
	}
	return v, true, nil
}

// deterministicTrackingID derives the tracking id from the idempotency key so every attempt (even
// one racing a crash) proposes the same value. Same shape as the legacy random ids.
func deterministicTrackingID(key string) string {
	h := sha256.Sum256([]byte(key))
	digits := func(from, n int) string {
		out := ""
		for i := 0; i < n; i++ {
			out += fmt.Sprintf("%d", int(h[from+i])%10)
		}
		return out
	}
	return fmt.Sprintf("%c%c-%d%s-%d%s", 65+rune(h[0])%25, 65+rune(h[1])%25, 12, digits(2, 3), 6, digits(5, 7))
}

// testFault returns the configured test-only fault (honoured ONLY with TELEMETRY_TEST_MODE=1).
func testFault() string {
	if os.Getenv("TELEMETRY_TEST_MODE") != "1" {
		return ""
	}
	return os.Getenv("SHIPPING_TEST_FAULT")
}
