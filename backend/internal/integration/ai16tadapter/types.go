// Package ai16tadapter defines the narrow boundary between Sub2API's control
// plane and the AI16T Commercial Core. Financial state never flows from
// Sub2API into the Core through this package.
package ai16tadapter

import (
	"context"
	"errors"
)

var (
	ErrInvalidRequest         = errors.New("ai16t adapter: invalid request")
	ErrUnauthorized           = errors.New("ai16t adapter: api key unauthorized")
	ErrUserDisabled           = errors.New("ai16t adapter: user disabled")
	ErrAPIKeyRevoked          = errors.New("ai16t adapter: api key revoked")
	ErrModelMappingMissing    = errors.New("ai16t adapter: model mapping missing")
	ErrInvalidAuthorityResult = errors.New("ai16t adapter: invalid commercial core result")
	ErrCoreExecutionFailed    = errors.New("ai16t adapter: commercial core execution failed")
	ErrProjectionDriftActive  = errors.New("ai16t adapter: projection drift blocks financial writes")
	ErrDriftStateUnavailable  = errors.New("ai16t adapter: projection drift state unavailable")
)

// Request contains only request-scoped data. RawAPIKey is used to create a
// keyed fingerprint and is never passed to a dependency or persisted.
type Request struct {
	RawAPIKey      string
	IdempotencyKey string
	RequestedModel string
	Payload        []byte
	// FailurePlan and ClientResponseDelayMS are accepted only by the isolated
	// hybrid runtime. Production handlers must reject both before Adapter.Execute.
	FailurePlan           map[string]string
	ClientResponseDelayMS int
}

// Principal is a non-financial identity projection from Sub2API.
type Principal struct {
	UserID          string
	APICredentialID string
	Group           string
	UserEnabled     bool
	KeyRevoked      bool
}

// ModelMapping maps the public model name to the Core-owned routing model.
// It contains no provider credential.
type ModelMapping struct {
	PublicModel string
	CoreModel   string
}

// CoreRequest is the complete input allowed to cross into the Commercial
// Core. It deliberately has no Sub2API balance, quota or price fields.
type CoreRequest struct {
	UserReference         string
	KeyReference          string
	IdempotencyKey        string
	RequestHash           string
	Model                 string
	Payload               []byte
	FailurePlan           map[string]string
	ClientResponseDelayMS int
}

type OutcomeStatus string

const (
	OutcomeSettled OutcomeStatus = "SETTLED"
	OutcomeFailed  OutcomeStatus = "FAILED_RELEASED"
)

// CoreResult is authoritative. Monetary values are integer micro-units.
type CoreResult struct {
	AuthoritativeRequestID string
	LedgerReference        string
	Status                 OutcomeStatus
	Response               []byte
	ContentType            string
	InputTokens            int64
	OutputTokens           int64
	CustomerChargeMicro    int64
	ProviderCostMicro      int64
	BalanceAfterMicro      int64
	RefundMicro            int64
	NetRevenueMicro        int64
	Replay                 bool
}

// CoreStreamChunk is one complete upstream SSE frame. Terminal is true only
// for the protocol completion frame, which callers hold until projection has
// been published after the authoritative Ledger settlement.
type CoreStreamChunk struct {
	Frame    []byte
	Terminal bool
}

// Projection is a read-only convenience view for Sub2API UI and reporting.
// Projection implementations must deduplicate on AuthoritativeRequestID.
type Projection struct {
	AuthoritativeRequestID string
	LedgerReference        string
	UserReference          string
	KeyReference           string
	Model                  string
	Status                 OutcomeStatus
	InputTokens            int64
	OutputTokens           int64
	CustomerChargeMicro    int64
	ProviderCostMicro      int64
	BalanceAfterMicro      int64
	RefundMicro            int64
	NetRevenueMicro        int64
	Replay                 bool
}

type Result struct {
	Core                 CoreResult
	FinanciallyCommitted bool
	ProjectionDrift      bool
	DriftStateRecorded   bool
}

// IdentitySource authenticates only a keyed fingerprint. The raw API key must
// never leave Adapter.Execute.
type IdentitySource interface {
	AuthenticateAPIKey(ctx context.Context, keyedFingerprint string) (Principal, error)
}

type ModelSource interface {
	ResolveModel(ctx context.Context, group, requestedModel string) (ModelMapping, error)
}

// CommercialCore is the only financial authority.
type CommercialCore interface {
	Execute(ctx context.Context, request CoreRequest) (CoreResult, error)
}

// StreamingCommercialCore extends the financial authority with a bounded
// streaming transport. onChunk must return only after the complete SSE frame
// has been written and flushed, so cancellation propagates synchronously.
type StreamingCommercialCore interface {
	CommercialCore
	ExecuteStream(
		ctx context.Context,
		request CoreRequest,
		onChunk func(CoreStreamChunk) error,
	) (CoreResult, error)
}

// ProjectionSink cannot mutate the Commercial Core. A write failure is
// reported as drift, never repaired by treating Sub2API as financial truth.
type ProjectionSink interface {
	Publish(ctx context.Context, projection Projection) error
}

// DriftGate is a durable reconciliation gate. Implementations must fail
// closed when their backing store is unavailable and keep a user blocked until
// an explicit Ledger-driven reconciliation clears the recorded drift.
type DriftGate interface {
	AllowFinancialWrite(ctx context.Context, userReference string) (bool, error)
	RecordProjectionDrift(ctx context.Context, projection Projection, cause error) error
	ReconcileProjection(ctx context.Context, projection Projection) error
}
