package routes

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/integration/ai16tadapter"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
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

func TestLoadAI16TModelMappingsCanaryRequiresExactApprovedSet(t *testing.T) {
	t.Setenv("AI16T_PROVIDER_MODE", "apiyi")
	t.Setenv("AI16T_CANARY_POLICY_ENABLED", "true")
	directory := t.TempDir()
	path := filepath.Join(directory, "models.json")
	t.Setenv("AI16T_MODEL_CONFIG_FILE", path)

	require.NoError(t, os.WriteFile(path, []byte(`{"models":[{"model":"deepseek-chat","upstream_model":"deepseek-chat"},{"model":"gpt-5.6-luna","upstream_model":"gpt-5.6-luna"}]}`), 0o600))
	mappings, err := loadAI16TModelMappings()
	require.NoError(t, err)
	require.Len(t, mappings, 2)

	require.NoError(t, os.WriteFile(path, []byte(`{"models":[{"model":"deepseek-chat","upstream_model":"deepseek-chat"},{"model":"unapproved","upstream_model":"unapproved"}]}`), 0o600))
	_, err = loadAI16TModelMappings()
	require.Error(t, err)
}

func TestValidFirstCanaryAPIKeyFailsClosed(t *testing.T) {
	now := time.Date(2026, time.August, 24, 20, 0, 0, 0, time.UTC)
	daily := 1.0
	created := now.Add(-time.Hour)
	expires := created.Add(7 * 24 * time.Hour)
	group := &service.Group{
		ID:                  41,
		Name:                firstCanaryGroupName,
		Platform:            service.PlatformOpenAI,
		IsExclusive:         true,
		Status:              service.StatusActive,
		Hydrated:            true,
		DailyLimitUSD:       &daily,
		DefaultValidityDays: 7,
		RPMLimit:            10,
		ModelsListConfig: service.GroupModelsListConfig{
			Enabled: true,
			Models:  append([]string(nil), firstCanaryModels...),
		},
	}
	key := &service.APIKey{
		ID:        51,
		Status:    service.StatusAPIKeyActive,
		CreatedAt: created,
		ExpiresAt: &expires,
		Group:     group,
		User: &service.User{
			ID:                  61,
			Status:              service.StatusActive,
			Concurrency:         1,
			AllowedGroups:       []int64{group.ID},
			APIKeyCount:         1,
			APIKeyCountResolved: true,
		},
	}
	require.True(t, validFirstCanaryAPIKey(key, now))

	tests := []struct {
		name   string
		mutate func()
	}{
		{"ungrouped", func() { key.Group = nil }},
		{"not manually approved", func() { key.User.AllowedGroups = nil }},
		{"wrong concurrency", func() { key.User.Concurrency = 2 }},
		{"unresolved key count", func() { key.User.APIKeyCountResolved = false }},
		{"multiple keys", func() { key.User.APIKeyCount = 2 }},
		{"untrusted group projection", func() { key.Group.Hydrated = false }},
		{"wrong model set", func() { key.Group.ModelsListConfig.Models = []string{"deepseek-chat", "other"} }},
		{"no expiry", func() { key.ExpiresAt = nil }},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			copyGroup := *group
			copyGroup.ModelsListConfig.Models = append([]string(nil), firstCanaryModels...)
			copyUser := *key.User
			copyUser.AllowedGroups = []int64{group.ID}
			copyKey := *key
			copyKey.Group = &copyGroup
			copyKey.User = &copyUser
			copyKey.ExpiresAt = &expires
			key = &copyKey
			test.mutate()
			require.False(t, validFirstCanaryAPIKey(key, now))
		})
	}
}

func TestAI16TAPIKeyRevocationIgnoresOnlySub2APIQuotaState(t *testing.T) {
	require.False(t, ai16tAPIKeyRevoked(service.StatusAPIKeyActive))
	require.False(t, ai16tAPIKeyRevoked(service.StatusAPIKeyQuotaExhausted))
	require.True(t, ai16tAPIKeyRevoked(service.StatusAPIKeyDisabled))
	require.True(t, ai16tAPIKeyRevoked(service.StatusAPIKeyExpired))
	require.True(t, ai16tAPIKeyRevoked("unknown"))
}

func TestAI16TSettledStreamReplayReturnsExplicitConflict(t *testing.T) {
	gin.SetMode(gin.TestMode)
	recorder := httptest.NewRecorder()
	context, _ := gin.CreateTestContext(recorder)
	result := ai16tadapter.Result{Core: ai16tadapter.CoreResult{
		AuthoritativeRequestID: "request-settled-replay",
		Status:                 ai16tadapter.OutcomeSettled,
		Replay:                 true,
	}}

	require.True(t, writeAI16TStreamReplayUnavailable(context, result))
	require.Equal(t, http.StatusConflict, recorder.Code)
	require.Equal(t, "true", recorder.Header().Get("X-Idempotency-Replayed"))
	require.Equal(t, "AI16T_COMMERCIAL_CORE", recorder.Header().Get("X-AI16T-Ledger-Authority"))
	var payload struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
		RequestID        string `json:"request_id"`
		SettlementStatus string `json:"settlement_status"`
	}
	require.NoError(t, json.Unmarshal(recorder.Body.Bytes(), &payload))
	require.Equal(t, "AI16T_STREAM_REPLAY_UNAVAILABLE", payload.Error.Code)
	require.Equal(t, "request-settled-replay", payload.RequestID)
	require.Equal(t, string(ai16tadapter.OutcomeSettled), payload.SettlementStatus)
}

func TestAI16TNonReplayWithoutFramesFallsThrough(t *testing.T) {
	gin.SetMode(gin.TestMode)
	recorder := httptest.NewRecorder()
	context, _ := gin.CreateTestContext(recorder)
	require.False(t, writeAI16TStreamReplayUnavailable(context, ai16tadapter.Result{}))
}
