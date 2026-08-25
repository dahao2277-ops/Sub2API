package routes

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/integration/ai16tadapter"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestAI16TAuthProbeFailsClosedForRejectedUpstreamStatus(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, upstreamStatus := range []int{
		http.StatusOK,
		http.StatusUnauthorized,
		http.StatusForbidden,
		http.StatusTooManyRequests,
		http.StatusFound,
	} {
		t.Run(http.StatusText(upstreamStatus), func(t *testing.T) {
			coreServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				require.Equal(t, http.MethodPost, r.Method)
				require.Equal(t, "/internal/v1/provider-auth-probe", r.URL.Path)
				_ = json.NewEncoder(w).Encode(ai16tadapter.AuthProbeResult{
					Endpoint:                   "https://api.apiyi.com/v1/models",
					UpstreamHTTPStatus:         upstreamStatus,
					ContentType:                "application/json",
					KeyFingerprint:             strings.Repeat("f", 64),
					AuthorizationHeaderPresent: true,
					BearerPrefixCorrect:        true,
					AuthorizationHeaderLength:  64,
				})
			}))
			defer coreServer.Close()

			core, err := ai16tadapter.NewHTTPCommercialCore(
				coreServer.URL,
				[]byte(strings.Repeat("a", 32)),
				coreServer.Client(),
			)
			require.NoError(t, err)
			handler := &ai16tHybridHandler{core: core}
			router := gin.New()
			router.POST("/probe", handler.authProbe)
			request := httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/probe", nil)
			recorder := httptest.NewRecorder()
			router.ServeHTTP(recorder, request)

			if upstreamStatus == http.StatusOK {
				require.Equal(t, http.StatusOK, recorder.Code)
				require.Contains(t, recorder.Body.String(), `"hop_2_sub2api_adapter":true`)
			} else {
				require.Equal(t, http.StatusBadGateway, recorder.Code)
				require.Contains(t, recorder.Body.String(), `"hop_2_sub2api_adapter":false`)
				require.NotContains(t, recorder.Body.String(), `"evidence"`)
			}
		})
	}
}
