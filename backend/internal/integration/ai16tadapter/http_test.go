package ai16tadapter

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

func TestHTTPCommercialCoreSignsCanonicalRequest(t *testing.T) {
	key := []byte(strings.Repeat("k", 32))
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		require.NoError(t, err)
		digest := sha256.Sum256(body)
		canonical := strings.Join([]string{
			r.Method,
			r.URL.RequestURI(),
			r.Header.Get("X-AI16T-Timestamp"),
			r.Header.Get("X-AI16T-Nonce"),
			hex.EncodeToString(digest[:]),
		}, "\n")
		mac := hmac.New(sha256.New, key)
		_, _ = mac.Write([]byte(canonical))
		require.True(t, hmac.Equal([]byte(r.Header.Get("X-AI16T-Signature")), []byte(hex.EncodeToString(mac.Sum(nil)))))
		_ = json.NewEncoder(w).Encode(CoreResult{
			AuthoritativeRequestID: "req-1",
			LedgerReference:        "ledger-1",
			Status:                 OutcomeSettled,
			CustomerChargeMicro:    100,
			ProviderCostMicro:      60,
			BalanceAfterMicro:      900,
			NetRevenueMicro:        100,
		})
	}))
	defer server.Close()

	client, err := NewHTTPCommercialCore(server.URL, key, server.Client())
	require.NoError(t, err)
	client.now = func() time.Time { return time.Unix(123, 0) }
	result, err := client.Execute(context.Background(), CoreRequest{IdempotencyKey: "idem-1"})
	require.NoError(t, err)
	require.Equal(t, "req-1", result.AuthoritativeRequestID)
}

func TestHTTPCommercialCoreStreamsCompleteFramesAndRequiresSettlement(t *testing.T) {
	key := []byte(strings.Repeat("s", 32))
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, coreExecuteStreamPath, r.URL.Path)
		body, err := io.ReadAll(r.Body)
		require.NoError(t, err)
		digest := sha256.Sum256(body)
		canonical := strings.Join([]string{
			r.Method,
			r.URL.RequestURI(),
			r.Header.Get("X-AI16T-Timestamp"),
			r.Header.Get("X-AI16T-Nonce"),
			hex.EncodeToString(digest[:]),
		}, "\n")
		mac := hmac.New(sha256.New, key)
		_, _ = mac.Write([]byte(canonical))
		require.Equal(t, hex.EncodeToString(mac.Sum(nil)), r.Header.Get("X-AI16T-Signature"))
		w.Header().Set("Content-Type", "text/event-stream")
		flusher, ok := w.(http.Flusher)
		require.True(t, ok)
		frame := base64.StdEncoding.EncodeToString([]byte("data: first\n\n"))
		_, _ = io.WriteString(w, "event: chunk\ndata: {\"frame\":\""+frame+"\"}\n\n")
		flusher.Flush()
		terminal := base64.StdEncoding.EncodeToString([]byte("data: [DONE]\n\n"))
		_, _ = io.WriteString(w, "event: terminal\ndata: {\"frame\":\""+terminal+"\"}\n\n")
		settlement, err := json.Marshal(CoreResult{
			AuthoritativeRequestID: "req-stream",
			LedgerReference:        "ledger-stream",
			Status:                 OutcomeSettled,
			InputTokens:            12,
			OutputTokens:           8,
			CustomerChargeMicro:    5,
			ProviderCostMicro:      3,
			BalanceAfterMicro:      95,
			NetRevenueMicro:        5,
		})
		require.NoError(t, err)
		_, _ = io.WriteString(w, "event: settlement\ndata: "+string(settlement)+"\n\n")
	}))
	defer server.Close()

	client, err := NewHTTPCommercialCore(server.URL, key, server.Client())
	require.NoError(t, err)
	var chunks []CoreStreamChunk
	result, err := client.ExecuteStream(
		context.Background(),
		CoreRequest{IdempotencyKey: "idem-stream"},
		func(chunk CoreStreamChunk) error {
			chunks = append(chunks, chunk)
			return nil
		},
	)
	require.NoError(t, err)
	require.Equal(t, "req-stream", result.AuthoritativeRequestID)
	require.Equal(t, []CoreStreamChunk{
		{Frame: []byte("data: first\n\n")},
		{Frame: []byte("data: [DONE]\n\n"), Terminal: true},
	}, chunks)
}

func TestKeyedFingerprintAndStaticIdentityFailClosed(t *testing.T) {
	key := []byte(strings.Repeat("f", 32))
	fingerprint, err := KeyedFingerprint("sk-test", key)
	require.NoError(t, err)
	source := StaticIdentitySource{Fingerprint: fingerprint, Principal: Principal{UserID: "7"}}
	_, err = source.AuthenticateAPIKey(context.Background(), "wrong")
	require.ErrorIs(t, err, ErrUnauthorized)
	principal, err := source.AuthenticateAPIKey(context.Background(), fingerprint)
	require.NoError(t, err)
	require.Equal(t, "7", principal.UserID)
}

func TestMapModelSourceUsesVerifiedPassThroughMappings(t *testing.T) {
	source, err := NewMapModelSource(map[string]string{
		"gpt-low":      "gpt-low",
		"gpt-balanced": "gpt-balanced",
	})
	require.NoError(t, err)
	require.Equal(t, []string{"gpt-balanced", "gpt-low"}, source.PublicModels())
	mapping, err := source.ResolveModel(context.Background(), "", "gpt-low")
	require.NoError(t, err)
	require.Equal(t, ModelMapping{PublicModel: "gpt-low", CoreModel: "gpt-low"}, mapping)
	_, err = source.ResolveModel(context.Background(), "", "not-allowed")
	require.ErrorIs(t, err, ErrModelMappingMissing)
}
