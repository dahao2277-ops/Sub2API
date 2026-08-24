package routes

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

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
