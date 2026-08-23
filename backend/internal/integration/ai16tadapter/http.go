package ai16tadapter

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"
)

const (
	coreExecutePath       = "/internal/v1/execute"
	coreProjectionPath    = "/internal/v1/projection"
	coreRefundPath        = "/internal/v1/refund"
	coreHealthPath        = "/internal/v1/health"
	defaultCoreBodyLimit  = 2 << 20
	minimumSigningKeySize = 32
)

// HTTPCommercialCore is a fail-closed, replay-resistant client for the pinned
// Commercial Core bridge. The signing key is never serialized into a request
// body or log field.
type HTTPCommercialCore struct {
	baseURL string
	key     []byte
	client  *http.Client
	now     func() time.Time
}

func NewHTTPCommercialCore(baseURL string, signingKey []byte, client *http.Client) (*HTTPCommercialCore, error) {
	baseURL = strings.TrimRight(strings.TrimSpace(baseURL), "/")
	if !strings.HasPrefix(baseURL, "http://") || len(signingKey) < minimumSigningKeySize {
		return nil, ErrInvalidRequest
	}
	if client == nil {
		client = &http.Client{Timeout: 20 * time.Second}
	}
	return &HTTPCommercialCore{
		baseURL: baseURL,
		key:     append([]byte(nil), signingKey...),
		client:  client,
		now:     time.Now,
	}, nil
}

func (c *HTTPCommercialCore) Execute(ctx context.Context, request CoreRequest) (CoreResult, error) {
	var result CoreResult
	if err := c.do(ctx, http.MethodPost, coreExecutePath, request, &result); err != nil {
		return CoreResult{}, err
	}
	return result, nil
}

func (c *HTTPCommercialCore) Ready(ctx context.Context) error {
	var result struct {
		Status string `json:"status"`
	}
	if err := c.do(ctx, http.MethodGet, coreHealthPath, nil, &result); err != nil {
		return err
	}
	if result.Status != "ok" {
		return errors.New("core is not ready")
	}
	return nil
}

func (c *HTTPCommercialCore) Projection(ctx context.Context, userReference string) (Projection, error) {
	var result Projection
	path := coreProjectionPath + "?user_reference=" + userReference
	if err := c.do(ctx, http.MethodGet, path, nil, &result); err != nil {
		return Projection{}, err
	}
	return result, nil
}

type RefundRequest struct {
	AuthoritativeRequestID string `json:"authoritative_request_id"`
	RefundID               string `json:"refund_id"`
	AmountMicro            *int64 `json:"amount_micro,omitempty"`
}

func (c *HTTPCommercialCore) Refund(ctx context.Context, request RefundRequest) (Projection, error) {
	var result Projection
	if err := c.do(ctx, http.MethodPost, coreRefundPath, request, &result); err != nil {
		return Projection{}, err
	}
	return result, nil
}

func (c *HTTPCommercialCore) do(ctx context.Context, method, path string, input, output any) error {
	body := []byte(nil)
	var err error
	if input != nil {
		body, err = json.Marshal(input)
		if err != nil {
			return fmt.Errorf("marshal core request: %w", err)
		}
	}
	timestamp := strconv.FormatInt(c.now().UTC().Unix(), 10)
	nonceBytes := make([]byte, 24)
	if _, err := rand.Read(nonceBytes); err != nil {
		return fmt.Errorf("create signing nonce: %w", err)
	}
	nonce := hex.EncodeToString(nonceBytes)
	digest := sha256.Sum256(body)
	canonical := strings.Join([]string{method, path, timestamp, nonce, hex.EncodeToString(digest[:])}, "\n")
	mac := hmac.New(sha256.New, c.key)
	_, _ = mac.Write([]byte(canonical))

	httpRequest, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("create core request: %w", err)
	}
	httpRequest.Header.Set("Content-Type", "application/json")
	httpRequest.Header.Set("X-AI16T-Timestamp", timestamp)
	httpRequest.Header.Set("X-AI16T-Nonce", nonce)
	httpRequest.Header.Set("X-AI16T-Signature", hex.EncodeToString(mac.Sum(nil)))

	response, err := c.client.Do(httpRequest)
	if err != nil {
		return fmt.Errorf("call core: %w", err)
	}
	defer response.Body.Close()
	limited := io.LimitReader(response.Body, defaultCoreBodyLimit+1)
	responseBody, err := io.ReadAll(limited)
	if err != nil {
		return fmt.Errorf("read core response: %w", err)
	}
	if len(responseBody) > defaultCoreBodyLimit {
		return errors.New("core response exceeds limit")
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("core rejected request with status %d", response.StatusCode)
	}
	if err := json.Unmarshal(responseBody, output); err != nil {
		return fmt.Errorf("decode core response: %w", err)
	}
	return nil
}

type StaticIdentitySource struct {
	Fingerprint string
	Principal   Principal
}

func (s StaticIdentitySource) AuthenticateAPIKey(_ context.Context, fingerprint string) (Principal, error) {
	if fingerprint == "" || !hmac.Equal([]byte(fingerprint), []byte(s.Fingerprint)) {
		return Principal{}, ErrUnauthorized
	}
	return s.Principal, nil
}

type StaticModelSource struct {
	PublicModel string
	CoreModel   string
}

func (s StaticModelSource) ResolveModel(_ context.Context, _ string, requested string) (ModelMapping, error) {
	if requested != s.PublicModel || strings.TrimSpace(s.CoreModel) == "" {
		return ModelMapping{}, ErrModelMappingMissing
	}
	return ModelMapping{PublicModel: requested, CoreModel: s.CoreModel}, nil
}

func KeyedFingerprint(raw string, key []byte) (string, error) {
	if len(key) < minimumFingerprintKeyBytes || strings.TrimSpace(raw) == "" {
		return "", ErrInvalidRequest
	}
	mac := hmac.New(sha256.New, key)
	_, _ = mac.Write([]byte(raw))
	return hex.EncodeToString(mac.Sum(nil)), nil
}
