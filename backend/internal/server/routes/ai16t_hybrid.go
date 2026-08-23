package routes

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/integration/ai16tadapter"
	"github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
)

const maxAI16TRequestBytes = 1 << 20

type ai16tHybridHandler struct {
	core           *ai16tadapter.HTTPCommercialCore
	projections    *ai16tadapter.RedisProjectionStore
	fingerprintKey []byte
	publicModel    string
	coreModel      string
	isolatedTest   bool
	emergencyGate  *ai16tadapter.EmergencyDriftGate
}

func RegisterAI16THybridRoutes(
	r *gin.Engine,
	apiKeyAuth middleware.APIKeyAuthMiddleware,
	adminAuth middleware.AdminAuthMiddleware,
	redisClient *redis.Client,
) error {
	if os.Getenv("AI16T_HYBRID_ENABLED") != "true" {
		return nil
	}
	signingKey, err := readMode0600Secret(os.Getenv("AI16T_CORE_SIGNING_KEY_FILE"))
	if err != nil {
		return fmt.Errorf("load core signing key: %w", err)
	}
	fingerprintKey, err := readMode0600Secret(os.Getenv("AI16T_FINGERPRINT_KEY_FILE"))
	if err != nil {
		return fmt.Errorf("load fingerprint key: %w", err)
	}
	core, err := ai16tadapter.NewHTTPCommercialCore(os.Getenv("AI16T_CORE_URL"), signingKey, nil)
	if err != nil {
		return fmt.Errorf("configure core client: %w", err)
	}
	projections, err := ai16tadapter.NewRedisProjectionStore(redisClient, "ai16t:projection:v1")
	if err != nil {
		return fmt.Errorf("configure projection store: %w", err)
	}
	handler := &ai16tHybridHandler{
		core:           core,
		projections:    projections,
		fingerprintKey: fingerprintKey,
		publicModel:    envOrDefault("AI16T_PUBLIC_MODEL", "ai16t-mock"),
		coreModel:      envOrDefault("AI16T_CORE_MODEL", "gpt-4o-mini"),
		isolatedTest:   isolatedTestHooksEnabled(),
		emergencyGate:  ai16tadapter.NewEmergencyDriftGate(),
	}

	r.GET("/ready", handler.ready)
	r.POST("/v1/ai16t/chat/completions", gin.HandlerFunc(apiKeyAuth), handler.execute)
	r.GET("/v1/ai16t/projection", gin.HandlerFunc(apiKeyAuth), handler.projection)
	admin := r.Group("/api/v1/admin/ai16t", gin.HandlerFunc(adminAuth))
	admin.POST("/reconcile", handler.reconcile)
	admin.POST("/refund", handler.refund)
	return nil
}

func (h *ai16tHybridHandler) ready(c *gin.Context) {
	ctx, cancel := context.WithTimeout(c.Request.Context(), 2*time.Second)
	defer cancel()
	if err := h.projections.Ready(ctx); err != nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"status": "not_ready", "component": "redis"})
		return
	}
	if err := h.core.Ready(ctx); err != nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"status": "not_ready", "component": "commercial_core"})
		return
	}
	c.JSON(http.StatusOK, gin.H{"status": "ok", "ledger_authority": "AI16T_COMMERCIAL_CORE"})
}

type ai16tOpenAIRequest struct {
	Model    string `json:"model"`
	Messages []struct {
		Role    string `json:"role"`
		Content string `json:"content"`
	} `json:"messages"`
}

func (h *ai16tHybridHandler) execute(c *gin.Context) {
	apiKey, ok := middleware.GetAPIKeyFromContext(c)
	if !ok || apiKey.User == nil {
		middleware.AbortWithError(c, http.StatusUnauthorized, "AI16T_AUTH_REQUIRED", "API key authentication required")
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(c.Writer, c.Request.Body, maxAI16TRequestBytes))
	if err != nil {
		middleware.AbortWithError(c, http.StatusBadRequest, "AI16T_INVALID_REQUEST", "request body is invalid")
		return
	}
	var request ai16tOpenAIRequest
	if err := json.Unmarshal(body, &request); err != nil || request.Model != h.publicModel || len(request.Messages) == 0 {
		middleware.AbortWithError(c, http.StatusBadRequest, "AI16T_INVALID_REQUEST", "model and messages are required")
		return
	}
	idempotencyKey := strings.TrimSpace(c.GetHeader("Idempotency-Key"))
	if idempotencyKey == "" {
		middleware.AbortWithError(c, http.StatusBadRequest, "AI16T_IDEMPOTENCY_REQUIRED", "Idempotency-Key is required")
		return
	}
	rawAPIKey := extractBearer(c.GetHeader("Authorization"))
	if rawAPIKey == "" {
		rawAPIKey = strings.TrimSpace(c.GetHeader("x-api-key"))
	}
	fingerprint, err := ai16tadapter.KeyedFingerprint(rawAPIKey, h.fingerprintKey)
	if err != nil {
		middleware.AbortWithError(c, http.StatusUnauthorized, "AI16T_AUTH_REQUIRED", "API key authentication required")
		return
	}
	group := ""
	if apiKey.Group != nil {
		group = strconv.FormatInt(apiKey.Group.ID, 10)
	}
	identity := ai16tadapter.StaticIdentitySource{
		Fingerprint: fingerprint,
		Principal: ai16tadapter.Principal{
			UserID:          strconv.FormatInt(apiKey.User.ID, 10),
			APICredentialID: strconv.FormatInt(apiKey.ID, 10),
			Group:           group,
			UserEnabled:     apiKey.User.IsActive(),
			KeyRevoked:      ai16tAPIKeyRevoked(apiKey.Status),
		},
	}
	adapter, err := ai16tadapter.NewWithEmergencyDriftGate(
		identity,
		ai16tadapter.StaticModelSource{PublicModel: h.publicModel, CoreModel: h.coreModel},
		h.core,
		h.projections,
		h.projections,
		h.fingerprintKey,
		h.emergencyGate,
	)
	if err != nil {
		middleware.AbortWithError(c, http.StatusServiceUnavailable, "AI16T_UNAVAILABLE", "hybrid adapter unavailable")
		return
	}
	requestContext := c.Request.Context()
	failurePlan := map[string]string(nil)
	clientDelay := 0
	if h.isolatedTest {
		if c.GetHeader("X-AI16T-Test-Projection-Failure") == "1" {
			requestContext = ai16tadapter.WithProjectionFailure(requestContext)
		}
		if raw := strings.TrimSpace(c.GetHeader("X-AI16T-Test-Failure-Plan")); raw != "" {
			if err := json.Unmarshal([]byte(raw), &failurePlan); err != nil {
				middleware.AbortWithError(c, http.StatusBadRequest, "AI16T_INVALID_TEST_PLAN", "invalid isolated failure plan")
				return
			}
		}
		clientDelay, _ = strconv.Atoi(c.GetHeader("X-AI16T-Test-Client-Delay-Ms"))
		if clientDelay < 0 || clientDelay > 5000 {
			middleware.AbortWithError(c, http.StatusBadRequest, "AI16T_INVALID_TEST_DELAY", "invalid isolated client delay")
			return
		}
	} else if c.GetHeader("X-AI16T-Test-Projection-Failure") != "" ||
		c.GetHeader("X-AI16T-Test-Failure-Plan") != "" ||
		c.GetHeader("X-AI16T-Test-Client-Delay-Ms") != "" {
		middleware.AbortWithError(c, http.StatusForbidden, "AI16T_TEST_HOOK_DISABLED", "isolated test hooks are disabled")
		return
	}
	result, err := adapter.Execute(requestContext, ai16tadapter.Request{
		RawAPIKey:             rawAPIKey,
		IdempotencyKey:        idempotencyKey,
		RequestedModel:        request.Model,
		Payload:               body,
		FailurePlan:           failurePlan,
		ClientResponseDelayMS: clientDelay,
	})
	if err != nil {
		status, code := hybridError(err)
		middleware.AbortWithError(c, status, code, code)
		return
	}
	if clientDelay > 0 {
		timer := time.NewTimer(time.Duration(clientDelay) * time.Millisecond)
		defer timer.Stop()
		select {
		case <-timer.C:
		case <-c.Request.Context().Done():
			return
		}
	}
	c.Header("X-AI16T-Ledger-Request-ID", result.Core.AuthoritativeRequestID)
	c.Header("X-AI16T-Ledger-Authority", "AI16T_COMMERCIAL_CORE")
	if result.Core.Replay {
		c.Header("X-AI16T-Idempotent-Replay", "true")
	}
	if result.ProjectionDrift {
		c.Header("X-AI16T-Projection-Drift", "PROJECTION_DRIFT")
	}
	if len(result.Core.Response) != 0 {
		c.Data(http.StatusOK, "application/json", result.Core.Response)
		return
	}
	c.JSON(http.StatusBadGateway, gin.H{"error": gin.H{"code": "AI16T_PROVIDER_FAILED", "message": "provider request failed"}})
}

func (h *ai16tHybridHandler) projection(c *gin.Context) {
	apiKey, ok := middleware.GetAPIKeyFromContext(c)
	if !ok || apiKey.User == nil {
		middleware.AbortWithError(c, http.StatusUnauthorized, "AI16T_AUTH_REQUIRED", "API key authentication required")
		return
	}
	projection, err := h.projections.Latest(c.Request.Context(), strconv.FormatInt(apiKey.User.ID, 10))
	if err != nil {
		middleware.AbortWithError(c, http.StatusNotFound, "AI16T_PROJECTION_NOT_FOUND", "projection is unavailable")
		return
	}
	c.JSON(http.StatusOK, gin.H{"authority": "LEDGER_WINS", "projection": projection})
}

type reconcileRequest struct {
	UserReference string `json:"user_reference"`
}

func (h *ai16tHybridHandler) reconcile(c *gin.Context) {
	var request reconcileRequest
	if err := c.ShouldBindJSON(&request); err != nil || strings.TrimSpace(request.UserReference) == "" {
		middleware.AbortWithError(c, http.StatusBadRequest, "AI16T_INVALID_REQUEST", "user_reference is required")
		return
	}
	projection, err := h.core.Projection(c.Request.Context(), request.UserReference)
	if err != nil || h.projections.ReconcileProjection(c.Request.Context(), projection) != nil {
		middleware.AbortWithError(c, http.StatusServiceUnavailable, "AI16T_RECONCILIATION_FAILED", "Ledger-driven reconciliation failed")
		return
	}
	h.emergencyGate.Clear(projection.UserReference)
	c.JSON(http.StatusOK, gin.H{"status": "reconciled", "authority": "LEDGER_WINS", "projection": projection})
}

func (h *ai16tHybridHandler) refund(c *gin.Context) {
	var request ai16tadapter.RefundRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		middleware.AbortWithError(c, http.StatusBadRequest, "AI16T_INVALID_REQUEST", "refund request is invalid")
		return
	}
	projection, err := h.core.Refund(c.Request.Context(), request)
	if err != nil || h.projections.ReconcileProjection(c.Request.Context(), projection) != nil {
		middleware.AbortWithError(c, http.StatusServiceUnavailable, "AI16T_REFUND_FAILED", "authoritative refund failed")
		return
	}
	c.JSON(http.StatusOK, gin.H{"status": "refunded", "authority": "LEDGER_WINS", "projection": projection})
}

func hybridError(err error) (int, string) {
	switch {
	case errors.Is(err, ai16tadapter.ErrProjectionDriftActive):
		return http.StatusConflict, "PROJECTION_DRIFT"
	case errors.Is(err, ai16tadapter.ErrUnauthorized), errors.Is(err, ai16tadapter.ErrAPIKeyRevoked), errors.Is(err, ai16tadapter.ErrUserDisabled):
		return http.StatusUnauthorized, "AI16T_UNAUTHORIZED"
	case errors.Is(err, ai16tadapter.ErrInvalidRequest), errors.Is(err, ai16tadapter.ErrModelMappingMissing):
		return http.StatusBadRequest, "AI16T_INVALID_REQUEST"
	default:
		return http.StatusServiceUnavailable, "AI16T_CORE_UNAVAILABLE"
	}
}

func extractBearer(value string) string {
	parts := strings.Fields(value)
	if len(parts) == 2 && strings.EqualFold(parts[0], "Bearer") {
		return parts[1]
	}
	return ""
}

func envOrDefault(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func isolatedTestHooksEnabled() bool {
	return os.Getenv("AI16T_ISOLATED_TEST_MODE") == "true" &&
		os.Getenv("AI16T_ISOLATED_TEST_HOOKS_ENABLED") == "true"
}

func ai16tAPIKeyRevoked(status string) bool {
	return status != service.StatusAPIKeyActive && status != service.StatusAPIKeyQuotaExhausted
}

func readMode0600Secret(path string) ([]byte, error) {
	path = strings.TrimSpace(path)
	if path == "" || !filepath.IsAbs(path) {
		return nil, errors.New("secret path must be absolute")
	}
	info, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm() != 0o600 {
		return nil, errors.New("secret must be a regular mode 0600 file")
	}
	value, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	value = []byte(strings.TrimSpace(string(value)))
	if len(value) < 32 {
		return nil, errors.New("secret must contain at least 32 bytes")
	}
	return value, nil
}

func randomRequestID() string {
	value := make([]byte, 16)
	_, _ = rand.Read(value)
	return fmt.Sprintf("sub2api-%x", value)
}
