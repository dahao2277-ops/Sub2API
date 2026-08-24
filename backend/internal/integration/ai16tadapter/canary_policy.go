package ai16tadapter

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
)

var (
	ErrCanaryDailyLimit        = errors.New("ai16t canary: daily limit reached")
	ErrCanaryProviderLimit     = errors.New("ai16t canary: provider limit reached")
	ErrCanaryRPM               = errors.New("ai16t canary: rpm limit reached")
	ErrCanaryConcurrency       = errors.New("ai16t canary: concurrency limit reached")
	ErrCanaryPolicyUnavailable = errors.New("ai16t canary: policy unavailable")
)

type CanaryPolicy struct {
	RPMLimit           int64
	ConcurrencyLimit   int64
	DailyLimitMicro    int64
	ProviderLimitMicro int64
	ReserveMicro       int64
	LeaseTTL           time.Duration
	Location           *time.Location
}

func (s *RedisProjectionStore) ConfigureCanaryPolicy(policy CanaryPolicy) error {
	if policy.RPMLimit < 1 || policy.ConcurrencyLimit != 1 || policy.DailyLimitMicro < 1 ||
		policy.ProviderLimitMicro < 1 || policy.ReserveMicro < 1 ||
		policy.ReserveMicro > policy.DailyLimitMicro || policy.ReserveMicro > policy.ProviderLimitMicro ||
		policy.LeaseTTL < time.Second || policy.Location == nil {
		return ErrInvalidRequest
	}
	s.canaryPolicy = &policy
	return nil
}

type CanaryLease struct {
	store *RedisProjectionStore
	key   string
	token string
}

var acquireCanaryLeaseScript = redis.NewScript(`
if redis.call('EXISTS', KEYS[1]) == 1 then return 2 end
if redis.call('EXISTS', KEYS[2]) == 1 then return -1 end
local provider = tonumber(redis.call('GET', KEYS[3]) or '0')
local daily = tonumber(redis.call('GET', KEYS[4]) or '0')
if provider + tonumber(ARGV[1]) > tonumber(ARGV[2]) then
  redis.call('SET', KEYS[2], 'provider_limit')
  return -2
end
if daily + tonumber(ARGV[1]) > tonumber(ARGV[3]) then return -3 end
if redis.call('EXISTS', KEYS[6]) == 1 then return -5 end
local rpm = redis.call('INCR', KEYS[5])
if rpm == 1 then redis.call('EXPIRE', KEYS[5], 60) end
if rpm > tonumber(ARGV[4]) then return -4 end
redis.call('SET', KEYS[6], ARGV[5], 'PX', ARGV[6])
return 1
`)

func (s *RedisProjectionStore) AcquireCanaryLease(
	ctx context.Context,
	userReference, idempotencyReference, token string,
) (*CanaryLease, error) {
	policy := s.canaryPolicy
	if policy == nil || userReference == "" || idempotencyReference == "" || token == "" {
		return nil, ErrCanaryPolicyUnavailable
	}
	now := s.now()
	day := now.In(policy.Location).Format("20060102")
	leaseKey := s.userKey(userReference, "canary:concurrency")
	keys := []string{
		s.userKey(userReference, "canary:settled:"+idempotencyReference),
		s.prefix + ":canary:blocked",
		s.prefix + ":canary:provider_total",
		s.userKey(userReference, "canary:daily:"+day),
		s.userKey(userReference, "canary:rpm:"+now.UTC().Format("200601021504")),
		leaseKey,
	}
	value, err := acquireCanaryLeaseScript.Run(ctx, s.client, keys,
		policy.ReserveMicro, policy.ProviderLimitMicro, policy.DailyLimitMicro,
		policy.RPMLimit, token, policy.LeaseTTL.Milliseconds()).Int64()
	if err != nil {
		return nil, ErrCanaryPolicyUnavailable
	}
	switch value {
	case 1:
		return &CanaryLease{store: s, key: leaseKey, token: token}, nil
	case 2:
		return &CanaryLease{}, nil
	case -1, -2:
		return nil, ErrCanaryProviderLimit
	case -3:
		return nil, ErrCanaryDailyLimit
	case -4:
		return nil, ErrCanaryRPM
	case -5:
		return nil, ErrCanaryConcurrency
	default:
		return nil, ErrCanaryPolicyUnavailable
	}
}

var releaseCanaryLeaseScript = redis.NewScript(`
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
`)

func (l *CanaryLease) Release(ctx context.Context) error {
	if l == nil || l.store == nil {
		return nil
	}
	return releaseCanaryLeaseScript.Run(ctx, l.store.client, []string{l.key}, l.token).Err()
}

var publishCanaryProjectionScript = redis.NewScript(`
if redis.call('EXISTS', KEYS[1]) == 1 then
  redis.call('SET', KEYS[2], ARGV[1], 'PX', ARGV[2])
  redis.call('SET', KEYS[3], '1', 'PX', ARGV[2])
  return 0
end
redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
redis.call('SET', KEYS[2], ARGV[1], 'PX', ARGV[2])
redis.call('SET', KEYS[3], '1', 'PX', ARGV[2])
if ARGV[3] == 'SETTLED' and ARGV[6] == '0' then
  local provider = redis.call('INCRBY', KEYS[4], ARGV[4])
  redis.call('INCRBY', KEYS[5], ARGV[5])
  redis.call('EXPIRE', KEYS[5], ARGV[7])
  if provider >= tonumber(ARGV[8]) then redis.call('SET', KEYS[6], 'provider_limit') end
end
return 1
`)

func (s *RedisProjectionStore) publishCanaryProjection(
	ctx context.Context,
	projection Projection,
	payload []byte,
) error {
	policy := s.canaryPolicy
	if policy == nil || projection.UserReference == "" || projection.AuthoritativeRequestID == "" ||
		projection.IdempotencyReference == "" {
		return ErrCanaryPolicyUnavailable
	}
	day := s.now().In(policy.Location).Format("20060102")
	keys := []string{
		s.userKey(projection.UserReference, "request:"+projection.AuthoritativeRequestID),
		s.userKey(projection.UserReference, "latest"),
		s.userKey(projection.UserReference, "canary:settled:"+projection.IdempotencyReference),
		s.prefix + ":canary:provider_total",
		s.userKey(projection.UserReference, "canary:daily:"+day),
		s.prefix + ":canary:blocked",
	}
	replay := int64(0)
	if projection.Replay {
		replay = 1
	}
	if err := publishCanaryProjectionScript.Run(ctx, s.client, keys,
		payload, projectionTTL.Milliseconds(), string(projection.Status),
		projection.ProviderCostMicro, projection.CustomerChargeMicro, replay,
		int64((48*time.Hour)/time.Second), policy.ProviderLimitMicro).Err(); err != nil {
		return fmt.Errorf("publish canary projection: %w", err)
	}
	return nil
}

func CanaryIdempotencyReference(idempotencyKey string) string {
	digest := sha256.Sum256([]byte(idempotencyKey))
	return hex.EncodeToString(digest[:])
}

func CanaryLeaseToken(idempotencyReference string, now time.Time) string {
	digest := sha256.Sum256([]byte(fmt.Sprintf("%s:%d", idempotencyReference, now.UnixNano())))
	return hex.EncodeToString(digest[:])
}
