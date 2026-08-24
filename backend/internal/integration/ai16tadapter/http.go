package ai16tadapter

import (
	"bufio"
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"time"
)

const (
	coreExecutePath       = "/internal/v1/execute"
	coreExecuteStreamPath = "/internal/v1/execute-stream"
	coreProjectionPath    = "/internal/v1/projection"
	coreRefundPath        = "/internal/v1/refund"
	coreHealthPath        = "/internal/v1/health"
	defaultCoreBodyLimit  = 2 << 20
	defaultCoreFrameLimit = 1 << 20
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
		// Request contexts and the Commercial Core's provider/stream duration
		// limits bound work. A client-wide deadline would truncate healthy SSE
		// responses whose total duration exceeds a non-stream request timeout.
		client = &http.Client{}
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

func (c *HTTPCommercialCore) ExecuteStream(
	ctx context.Context,
	request CoreRequest,
	onChunk func(CoreStreamChunk) error,
) (CoreResult, error) {
	if onChunk == nil {
		return CoreResult{}, ErrInvalidRequest
	}
	httpRequest, err := c.newSignedRequest(ctx, http.MethodPost, coreExecuteStreamPath, request)
	if err != nil {
		return CoreResult{}, err
	}
	response, err := c.client.Do(httpRequest)
	if err != nil {
		return CoreResult{}, fmt.Errorf("call core stream: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return CoreResult{}, fmt.Errorf("core rejected stream request with status %d", response.StatusCode)
	}
	if !strings.HasPrefix(strings.ToLower(response.Header.Get("Content-Type")), "text/event-stream") {
		return CoreResult{}, errors.New("core stream response has invalid content type")
	}

	scanner := bufio.NewScanner(response.Body)
	scanner.Buffer(make([]byte, 4096), defaultCoreFrameLimit*2)
	event := ""
	data := ""
	settled := false
	var result CoreResult
	handle := func() error {
		if event == "" && data == "" {
			return nil
		}
		if settled {
			return errors.New("core emitted data after settlement")
		}
		switch event {
		case "chunk", "terminal":
			var envelope struct {
				Frame string `json:"frame"`
			}
			if err := json.Unmarshal([]byte(data), &envelope); err != nil || envelope.Frame == "" {
				return errors.New("core emitted an invalid stream frame")
			}
			frame, err := base64.StdEncoding.DecodeString(envelope.Frame)
			if err != nil || len(frame) == 0 || len(frame) > defaultCoreFrameLimit {
				return errors.New("core emitted an invalid stream frame")
			}
			if err := onChunk(CoreStreamChunk{Frame: frame, Terminal: event == "terminal"}); err != nil {
				return fmt.Errorf("write downstream stream frame: %w", err)
			}
		case "settlement":
			if err := json.Unmarshal([]byte(data), &result); err != nil {
				return errors.New("core emitted an invalid settlement")
			}
			settled = true
		default:
			return errors.New("core emitted an unknown stream event")
		}
		return nil
	}
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			if err := handle(); err != nil {
				return CoreResult{}, err
			}
			event, data = "", ""
			continue
		}
		if strings.HasPrefix(line, ":") {
			continue
		}
		if strings.HasPrefix(line, "event:") {
			event = strings.TrimSpace(strings.TrimPrefix(line, "event:"))
			continue
		}
		if strings.HasPrefix(line, "data:") {
			value := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
			if data != "" {
				data += "\n"
			}
			data += value
		}
	}
	if err := scanner.Err(); err != nil {
		return CoreResult{}, fmt.Errorf("read core stream: %w", err)
	}
	if event != "" || data != "" {
		if err := handle(); err != nil {
			return CoreResult{}, err
		}
	}
	if !settled {
		return CoreResult{}, errors.New("core stream ended without settlement")
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
	httpRequest, err := c.newSignedRequest(ctx, method, path, input)
	if err != nil {
		return err
	}

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

func (c *HTTPCommercialCore) newSignedRequest(
	ctx context.Context,
	method, path string,
	input any,
) (*http.Request, error) {
	body := []byte(nil)
	var err error
	if input != nil {
		body, err = json.Marshal(input)
		if err != nil {
			return nil, fmt.Errorf("marshal core request: %w", err)
		}
	}
	timestamp := strconv.FormatInt(c.now().UTC().Unix(), 10)
	nonceBytes := make([]byte, 24)
	if _, err := rand.Read(nonceBytes); err != nil {
		return nil, fmt.Errorf("create signing nonce: %w", err)
	}
	nonce := hex.EncodeToString(nonceBytes)
	digest := sha256.Sum256(body)
	canonical := strings.Join([]string{method, path, timestamp, nonce, hex.EncodeToString(digest[:])}, "\n")
	mac := hmac.New(sha256.New, c.key)
	_, _ = mac.Write([]byte(canonical))

	httpRequest, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("create core request: %w", err)
	}
	httpRequest.Header.Set("Content-Type", "application/json")
	httpRequest.Header.Set("X-AI16T-Timestamp", timestamp)
	httpRequest.Header.Set("X-AI16T-Nonce", nonce)
	httpRequest.Header.Set("X-AI16T-Signature", hex.EncodeToString(mac.Sum(nil)))
	return httpRequest, nil
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

type MapModelSource struct {
	mappings map[string]string
}

func NewMapModelSource(mappings map[string]string) (*MapModelSource, error) {
	if len(mappings) == 0 || len(mappings) > 6 {
		return nil, ErrInvalidRequest
	}
	cloned := make(map[string]string, len(mappings))
	for publicModel, coreModel := range mappings {
		publicModel = strings.TrimSpace(publicModel)
		coreModel = strings.TrimSpace(coreModel)
		if publicModel == "" || coreModel == "" {
			return nil, ErrInvalidRequest
		}
		cloned[publicModel] = coreModel
	}
	return &MapModelSource{mappings: cloned}, nil
}

func (s *MapModelSource) ResolveModel(_ context.Context, _ string, requested string) (ModelMapping, error) {
	coreModel, ok := s.mappings[requested]
	if !ok {
		return ModelMapping{}, ErrModelMappingMissing
	}
	return ModelMapping{PublicModel: requested, CoreModel: coreModel}, nil
}

func (s *MapModelSource) PublicModels() []string {
	models := make([]string, 0, len(s.mappings))
	for model := range s.mappings {
		models = append(models, model)
	}
	slices.Sort(models)
	return models
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
