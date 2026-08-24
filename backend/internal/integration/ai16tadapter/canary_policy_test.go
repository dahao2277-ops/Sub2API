package ai16tadapter

import (
	"context"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
	"github.com/stretchr/testify/require"
)

func newCanaryStore(t *testing.T, policy CanaryPolicy) (*RedisProjectionStore, *redis.Client) {
	t.Helper()
	mini := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: mini.Addr()})
	store, err := NewRedisProjectionStore(client, "ai16t:canary-test")
	require.NoError(t, err)
	store.now = func() time.Time {
		return time.Date(2026, time.August, 24, 20, 0, 0, 0, time.FixedZone("MYT", 8*60*60))
	}
	require.NoError(t, store.ConfigureCanaryPolicy(policy))
	return store, client
}

func canaryTestPolicy() CanaryPolicy {
	return CanaryPolicy{
		RPMLimit:           10,
		ConcurrencyLimit:   1,
		DailyLimitMicro:    1_000_000,
		ProviderLimitMicro: 1_000_000,
		ReserveMicro:       250_000,
		LeaseTTL:           time.Minute,
		Location:           time.FixedZone("MYT", 8*60*60),
	}
}

func canaryProjection(requestID, idempotencyKey, user string, provider, customer int64) Projection {
	return Projection{
		AuthoritativeRequestID: requestID,
		IdempotencyReference:   CanaryIdempotencyReference(idempotencyKey),
		LedgerReference:        "ledger-" + requestID,
		UserReference:          user,
		Status:                 OutcomeSettled,
		ProviderCostMicro:      provider,
		CustomerChargeMicro:    customer,
	}
}

func TestCanaryPolicyRequiresConfiguredFailClosedLimits(t *testing.T) {
	mini := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: mini.Addr()})
	store, err := NewRedisProjectionStore(client, "ai16t:canary-test")
	require.NoError(t, err)
	_, err = store.AcquireCanaryLease(context.Background(), "user-1", "idem", "token")
	require.ErrorIs(t, err, ErrCanaryPolicyUnavailable)

	invalid := canaryTestPolicy()
	invalid.ConcurrencyLimit = 2
	require.ErrorIs(t, store.ConfigureCanaryPolicy(invalid), ErrInvalidRequest)
}

func TestCanaryConcurrencyAndRPMAreEnforced(t *testing.T) {
	policy := canaryTestPolicy()
	policy.RPMLimit = 2
	store, _ := newCanaryStore(t, policy)
	ctx := context.Background()

	lease, err := store.AcquireCanaryLease(ctx, "user-1", CanaryIdempotencyReference("idem-1"), "token-1")
	require.NoError(t, err)
	_, err = store.AcquireCanaryLease(ctx, "user-1", CanaryIdempotencyReference("idem-2"), "token-2")
	require.ErrorIs(t, err, ErrCanaryConcurrency)
	require.NoError(t, lease.Release(ctx))

	lease, err = store.AcquireCanaryLease(ctx, "user-1", CanaryIdempotencyReference("idem-2"), "token-2")
	require.NoError(t, err)
	require.NoError(t, lease.Release(ctx))
	_, err = store.AcquireCanaryLease(ctx, "user-1", CanaryIdempotencyReference("idem-3"), "token-3")
	require.ErrorIs(t, err, ErrCanaryRPM)
}

func TestCanaryProjectionIsAtomicIdempotentAndReplaysRemainAvailable(t *testing.T) {
	store, client := newCanaryStore(t, canaryTestPolicy())
	ctx := context.Background()
	projection := canaryProjection("request-1", "idem-1", "user-1", 1_000_000, 300_000)

	require.NoError(t, store.Publish(ctx, projection))
	require.NoError(t, store.Publish(ctx, projection))
	provider, err := client.Get(ctx, store.prefix+":canary:provider_total").Int64()
	require.NoError(t, err)
	require.Equal(t, int64(1_000_000), provider)
	daily, err := client.Get(ctx, store.userKey("user-1", "canary:daily:20260824")).Int64()
	require.NoError(t, err)
	require.Equal(t, int64(300_000), daily)

	settledLease, err := store.AcquireCanaryLease(
		ctx, "user-1", CanaryIdempotencyReference("idem-1"), "replay-token",
	)
	require.NoError(t, err, "a settled idempotent replay must remain available after the global stop")
	require.NoError(t, settledLease.Release(ctx))
	_, err = store.AcquireCanaryLease(
		ctx, "user-1", CanaryIdempotencyReference("idem-new"), "new-token",
	)
	require.ErrorIs(t, err, ErrCanaryProviderLimit)
}

func TestCanaryDailyAndProviderReserveGatesAreIndependent(t *testing.T) {
	t.Run("daily customer charge", func(t *testing.T) {
		store, _ := newCanaryStore(t, canaryTestPolicy())
		ctx := context.Background()
		require.NoError(t, store.Publish(ctx, canaryProjection("request-1", "idem-1", "user-1", 100_000, 800_000)))
		_, err := store.AcquireCanaryLease(ctx, "user-1", CanaryIdempotencyReference("idem-2"), "token-2")
		require.ErrorIs(t, err, ErrCanaryDailyLimit)
	})

	t.Run("global provider cost", func(t *testing.T) {
		store, _ := newCanaryStore(t, canaryTestPolicy())
		ctx := context.Background()
		require.NoError(t, store.Publish(ctx, canaryProjection("request-1", "idem-1", "user-1", 800_000, 100_000)))
		_, err := store.AcquireCanaryLease(ctx, "user-2", CanaryIdempotencyReference("idem-2"), "token-2")
		require.ErrorIs(t, err, ErrCanaryProviderLimit)
	})
}

func TestCanaryReconcileUpdatesProjectionWithoutDoubleCounting(t *testing.T) {
	store, client := newCanaryStore(t, canaryTestPolicy())
	ctx := context.Background()
	projection := canaryProjection("request-1", "idem-1", "user-1", 120_000, 150_000)
	require.NoError(t, store.Publish(ctx, projection))
	require.NoError(t, store.RecordProjectionDrift(ctx, projection, nil))

	projection.RefundMicro = 20_000
	projection.NetRevenueMicro = 130_000
	projection.Replay = true
	require.NoError(t, store.ReconcileProjection(ctx, projection))
	provider, err := client.Get(ctx, store.prefix+":canary:provider_total").Int64()
	require.NoError(t, err)
	require.Equal(t, int64(120_000), provider)
	latest, err := store.Latest(ctx, "user-1")
	require.NoError(t, err)
	require.Equal(t, int64(20_000), latest.RefundMicro)
	allowed, err := store.AllowFinancialWrite(ctx, "user-1")
	require.NoError(t, err)
	require.True(t, allowed)
}
