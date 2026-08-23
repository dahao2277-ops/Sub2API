package ai16tadapter

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

const projectionTTL = 30 * 24 * time.Hour

type projectionFailureKey struct{}

// WithProjectionFailure is available only to the isolated runtime test hook.
func WithProjectionFailure(ctx context.Context) context.Context {
	return context.WithValue(ctx, projectionFailureKey{}, true)
}

type RedisProjectionStore struct {
	client *redis.Client
	prefix string
}

func NewRedisProjectionStore(client *redis.Client, prefix string) (*RedisProjectionStore, error) {
	if client == nil || strings.TrimSpace(prefix) == "" {
		return nil, ErrInvalidRequest
	}
	return &RedisProjectionStore{client: client, prefix: strings.TrimSuffix(prefix, ":")}, nil
}

func (s *RedisProjectionStore) Ready(ctx context.Context) error {
	return s.client.Ping(ctx).Err()
}

func (s *RedisProjectionStore) userKey(userReference, suffix string) string {
	return fmt.Sprintf("%s:{%s}:%s", s.prefix, userReference, suffix)
}

func (s *RedisProjectionStore) Publish(ctx context.Context, projection Projection) error {
	if failed, _ := ctx.Value(projectionFailureKey{}).(bool); failed {
		return errors.New("isolated projection failure")
	}
	payload, err := json.Marshal(projection)
	if err != nil {
		return err
	}
	result, err := s.client.SetNX(
		ctx,
		s.userKey(projection.UserReference, "request:"+projection.AuthoritativeRequestID),
		payload,
		projectionTTL,
	).Result()
	if err != nil {
		return err
	}
	if result {
		return s.client.Set(ctx, s.userKey(projection.UserReference, "latest"), payload, projectionTTL).Err()
	}
	return nil
}

func (s *RedisProjectionStore) AllowFinancialWrite(ctx context.Context, userReference string) (bool, error) {
	drift, err := s.client.Exists(ctx, s.userKey(userReference, "drift")).Result()
	if err != nil {
		return false, err
	}
	return drift == 0, nil
}

func (s *RedisProjectionStore) RecordProjectionDrift(ctx context.Context, projection Projection, cause error) error {
	payload, err := json.Marshal(map[string]any{
		"code":                     "PROJECTION_DRIFT",
		"authority":                "LEDGER_WINS",
		"authoritative_request_id": projection.AuthoritativeRequestID,
		"recorded_at":              time.Now().UTC().Format(time.RFC3339Nano),
	})
	if err != nil {
		return err
	}
	return s.client.Set(ctx, s.userKey(projection.UserReference, "drift"), payload, 0).Err()
}

var reconcileProjectionScript = redis.NewScript(`
redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
redis.call('SET', KEYS[2], ARGV[1], 'PX', ARGV[2], 'NX')
redis.call('DEL', KEYS[3])
return 1
`)

func (s *RedisProjectionStore) ReconcileProjection(ctx context.Context, projection Projection) error {
	payload, err := json.Marshal(projection)
	if err != nil {
		return err
	}
	keys := []string{
		s.userKey(projection.UserReference, "latest"),
		s.userKey(projection.UserReference, "request:"+projection.AuthoritativeRequestID),
		s.userKey(projection.UserReference, "drift"),
	}
	return reconcileProjectionScript.Run(ctx, s.client, keys, payload, projectionTTL.Milliseconds()).Err()
}

func (s *RedisProjectionStore) Latest(ctx context.Context, userReference string) (Projection, error) {
	payload, err := s.client.Get(ctx, s.userKey(userReference, "latest")).Bytes()
	if err != nil {
		return Projection{}, err
	}
	var projection Projection
	if err := json.Unmarshal(payload, &projection); err != nil {
		return Projection{}, err
	}
	return projection, nil
}
