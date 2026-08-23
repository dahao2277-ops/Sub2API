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
)

// Request contains only request-scoped data. RawAPIKey is used to create a
// keyed fingerprint and is never passed to a dependency or persisted.
type Request struct {
	RawAPIKey      string
	IdempotencyKey string
	RequestedModel string
	RequestHash    string
	Payload        []byte
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
	UserReference  string
	KeyReference   string
	IdempotencyKey string
	RequestHash    string
	Model          string
	Payload        []byte
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
	InputTokens            int64
	OutputTokens           int64
	CustomerChargeMicro    int64
	ProviderCostMicro      int64
	BalanceAfterMicro      int64
	Replay                 bool
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
	Replay                 bool
}

type Result struct {
	Core            CoreResult
	ProjectionDrift bool
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

// ProjectionSink cannot mutate the Commercial Core. A write failure is
// reported as drift, never repaired by treating Sub2API as financial truth.
type ProjectionSink interface {
	Publish(ctx context.Context, projection Projection) error
}

type DriftReporter interface {
	ReportProjectionDrift(ctx context.Context, projection Projection, cause error)
}
