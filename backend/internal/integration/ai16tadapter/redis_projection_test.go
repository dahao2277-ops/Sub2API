package ai16tadapter

import (
	"context"
	"testing"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
	"github.com/stretchr/testify/require"
)

func TestRedisProjectionDriftBlocksUntilAuthoritativeReconcile(t *testing.T) {
	mini := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: mini.Addr()})
	store, err := NewRedisProjectionStore(client, "ai16t:test")
	require.NoError(t, err)
	projection := Projection{
		AuthoritativeRequestID: "request-1",
		LedgerReference:        "ledger-1",
		UserReference:          "user-1",
		BalanceAfterMicro:      900,
		NetRevenueMicro:        100,
	}
	require.NoError(t, store.Publish(context.Background(), projection))
	require.NoError(t, store.Publish(context.Background(), projection))
	require.NoError(t, store.RecordProjectionDrift(context.Background(), projection, nil))
	allowed, err := store.AllowFinancialWrite(context.Background(), "user-1")
	require.NoError(t, err)
	require.False(t, allowed)

	projection.BalanceAfterMicro = 950
	require.NoError(t, store.ReconcileProjection(context.Background(), projection))
	allowed, err = store.AllowFinancialWrite(context.Background(), "user-1")
	require.NoError(t, err)
	require.True(t, allowed)
	latest, err := store.Latest(context.Background(), "user-1")
	require.NoError(t, err)
	require.Equal(t, int64(950), latest.BalanceAfterMicro)
}

func TestRedisProjectionFailureHookIsContextScoped(t *testing.T) {
	mini := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: mini.Addr()})
	store, err := NewRedisProjectionStore(client, "ai16t:test")
	require.NoError(t, err)
	err = store.Publish(WithProjectionFailure(context.Background()), Projection{})
	require.Error(t, err)
}
