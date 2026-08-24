package routes

import (
	"os"
	"path/filepath"
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

func TestLoadAI16TModelMappingsRequiresMode0600PassThroughConfig(t *testing.T) {
	t.Setenv("AI16T_PROVIDER_MODE", "apiyi")
	directory := t.TempDir()
	path := filepath.Join(directory, "models.json")
	payload := []byte(`{"models":[{"model":"gpt-low","upstream_model":"gpt-low"},{"model":"gpt-balanced","upstream_model":"gpt-balanced"}]}`)
	require.NoError(t, os.WriteFile(path, payload, 0o600))
	t.Setenv("AI16T_MODEL_CONFIG_FILE", path)
	mappings, err := loadAI16TModelMappings()
	require.NoError(t, err)
	require.Equal(t, map[string]string{
		"gpt-low":      "gpt-low",
		"gpt-balanced": "gpt-balanced",
	}, mappings)

	require.NoError(t, os.Chmod(path, 0o644))
	_, err = loadAI16TModelMappings()
	require.Error(t, err)
}

func TestAI16TAPIKeyRevocationIgnoresOnlySub2APIQuotaState(t *testing.T) {
	require.False(t, ai16tAPIKeyRevoked(service.StatusAPIKeyActive))
	require.False(t, ai16tAPIKeyRevoked(service.StatusAPIKeyQuotaExhausted))
	require.True(t, ai16tAPIKeyRevoked(service.StatusAPIKeyDisabled))
	require.True(t, ai16tAPIKeyRevoked(service.StatusAPIKeyExpired))
	require.True(t, ai16tAPIKeyRevoked("unknown"))
}
