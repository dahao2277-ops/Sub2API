package ai16tadapter

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
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
