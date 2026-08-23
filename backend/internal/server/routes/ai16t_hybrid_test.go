package routes

import (
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

func TestIsolatedTestHooksRequireBothGates(t *testing.T) {
	t.Setenv("AI16T_ISOLATED_TEST_MODE", "")
	t.Setenv("AI16T_ISOLATED_TEST_HOOKS_ENABLED", "")
	require.False(t, isolatedTestHooksEnabled())

	t.Setenv("AI16T_ISOLATED_TEST_MODE", "true")
	require.False(t, isolatedTestHooksEnabled())

	t.Setenv("AI16T_ISOLATED_TEST_MODE", "")
	t.Setenv("AI16T_ISOLATED_TEST_HOOKS_ENABLED", "true")
	require.False(t, isolatedTestHooksEnabled())

	t.Setenv("AI16T_ISOLATED_TEST_MODE", "true")
	require.True(t, isolatedTestHooksEnabled())
}

func TestAI16TAPIKeyRevocationIgnoresOnlySub2APIQuotaState(t *testing.T) {
	require.False(t, ai16tAPIKeyRevoked(service.StatusAPIKeyActive))
	require.False(t, ai16tAPIKeyRevoked(service.StatusAPIKeyQuotaExhausted))
	require.True(t, ai16tAPIKeyRevoked(service.StatusAPIKeyDisabled))
	require.True(t, ai16tAPIKeyRevoked(service.StatusAPIKeyExpired))
	require.True(t, ai16tAPIKeyRevoked("unknown"))
}
