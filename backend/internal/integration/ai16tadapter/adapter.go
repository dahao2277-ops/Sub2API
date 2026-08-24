package ai16tadapter

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"strings"
	"sync"
	"time"
)

const minimumFingerprintKeyBytes = 32
const postSettlementDriftTimeout = 3 * time.Second

type Adapter struct {
	identity       IdentitySource
	models         ModelSource
	core           CommercialCore
	projections    ProjectionSink
	driftGate      DriftGate
	fingerprintKey []byte
	emergencyGate  *EmergencyDriftGate
}

// EmergencyDriftGate is the process-local fail-closed backstop used when both
// the projection write and the durable drift marker write fail. It must be
// shared by every Adapter instance serving the same process so a subsequent
// request cannot forget an unresolved post-settlement drift.
type EmergencyDriftGate struct {
	driftedUsers sync.Map
}

func NewEmergencyDriftGate() *EmergencyDriftGate {
	return &EmergencyDriftGate{}
}

func (g *EmergencyDriftGate) blocked(userReference string) bool {
	if g == nil {
		return true
	}
	_, blocked := g.driftedUsers.Load(userReference)
	return blocked
}

func (g *EmergencyDriftGate) block(userReference string) {
	if g != nil {
		g.driftedUsers.Store(userReference, struct{}{})
	}
}

// Clear is intentionally called only after a fresh Ledger projection has
// atomically replaced the stale projection and cleared the durable drift gate.
func (g *EmergencyDriftGate) Clear(userReference string) {
	if g != nil {
		g.driftedUsers.Delete(userReference)
	}
}

func New(
	identity IdentitySource,
	models ModelSource,
	core CommercialCore,
	projections ProjectionSink,
	driftGate DriftGate,
	fingerprintKey []byte,
) (*Adapter, error) {
	return NewWithEmergencyDriftGate(
		identity,
		models,
		core,
		projections,
		driftGate,
		fingerprintKey,
		NewEmergencyDriftGate(),
	)
}

func NewWithEmergencyDriftGate(
	identity IdentitySource,
	models ModelSource,
	core CommercialCore,
	projections ProjectionSink,
	driftGate DriftGate,
	fingerprintKey []byte,
	emergencyGate *EmergencyDriftGate,
) (*Adapter, error) {
	if identity == nil || models == nil || core == nil || projections == nil || driftGate == nil {
		return nil, fmt.Errorf("%w: dependencies are required", ErrInvalidRequest)
	}
	if emergencyGate == nil {
		return nil, fmt.Errorf("%w: emergency drift gate is required", ErrInvalidRequest)
	}
	if len(fingerprintKey) < minimumFingerprintKeyBytes {
		return nil, fmt.Errorf("%w: fingerprint key must contain at least %d bytes", ErrInvalidRequest, minimumFingerprintKeyBytes)
	}

	return &Adapter{
		identity:       identity,
		models:         models,
		core:           core,
		projections:    projections,
		driftGate:      driftGate,
		fingerprintKey: append([]byte(nil), fingerprintKey...),
		emergencyGate:  emergencyGate,
	}, nil
}

// Execute authenticates Sub2API identity and model projections, then hands the
// request to the Commercial Core. The adapter never reads or writes a Sub2API
// balance as financial state.
func (a *Adapter) Execute(ctx context.Context, request Request) (Result, error) {
	if err := validateRequest(request); err != nil {
		return Result{}, err
	}

	principal, err := a.identity.AuthenticateAPIKey(ctx, a.fingerprint(request.RawAPIKey))
	if err != nil {
		return Result{}, ErrUnauthorized
	}
	if principal.KeyRevoked {
		return Result{}, ErrAPIKeyRevoked
	}
	if !principal.UserEnabled {
		return Result{}, ErrUserDisabled
	}
	if strings.TrimSpace(principal.UserID) == "" || strings.TrimSpace(principal.APICredentialID) == "" {
		return Result{}, ErrUnauthorized
	}
	if a.emergencyGate.blocked(principal.UserID) {
		return Result{}, ErrProjectionDriftActive
	}
	allowed, err := a.driftGate.AllowFinancialWrite(ctx, principal.UserID)
	if err != nil {
		return Result{}, ErrDriftStateUnavailable
	}
	if !allowed {
		return Result{}, ErrProjectionDriftActive
	}

	mapping, err := a.models.ResolveModel(ctx, principal.Group, request.RequestedModel)
	if err != nil || strings.TrimSpace(mapping.CoreModel) == "" {
		return Result{}, ErrModelMappingMissing
	}

	coreResult, err := a.core.Execute(ctx, CoreRequest{
		UserReference:         principal.UserID,
		KeyReference:          principal.APICredentialID,
		IdempotencyKey:        request.IdempotencyKey,
		RequestHash:           canonicalRequestHash(principal, mapping.CoreModel, request.Payload),
		Model:                 mapping.CoreModel,
		Payload:               append([]byte(nil), request.Payload...),
		FailurePlan:           cloneFailurePlan(request.FailurePlan),
		ClientResponseDelayMS: request.ClientResponseDelayMS,
	})
	if err != nil {
		return Result{}, ErrCoreExecutionFailed
	}
	if err := validateCoreResult(coreResult); err != nil {
		return Result{}, err
	}

	projection := Projection{
		AuthoritativeRequestID: coreResult.AuthoritativeRequestID,
		LedgerReference:        coreResult.LedgerReference,
		UserReference:          principal.UserID,
		KeyReference:           principal.APICredentialID,
		Model:                  mapping.CoreModel,
		Status:                 coreResult.Status,
		InputTokens:            coreResult.InputTokens,
		OutputTokens:           coreResult.OutputTokens,
		CustomerChargeMicro:    coreResult.CustomerChargeMicro,
		ProviderCostMicro:      coreResult.ProviderCostMicro,
		BalanceAfterMicro:      coreResult.BalanceAfterMicro,
		RefundMicro:            coreResult.RefundMicro,
		NetRevenueMicro:        coreResult.NetRevenueMicro,
		Replay:                 coreResult.Replay,
	}

	result := Result{
		Core:                 cloneCoreResult(coreResult),
		FinanciallyCommitted: coreResult.Status == OutcomeSettled,
	}
	if err := a.projections.Publish(ctx, projection); err != nil {
		result.ProjectionDrift = true
		a.emergencyGate.block(principal.UserID)
		recordCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), postSettlementDriftTimeout)
		defer cancel()
		if recordErr := a.driftGate.RecordProjectionDrift(recordCtx, projection, err); recordErr != nil {
			// The authoritative financial result has already committed. Returning a
			// transport error here would invite unsafe client retries, so surface
			// the partial-success state in Result and keep the local emergency
			// block until explicit reconciliation.
			return result, nil
		}
		result.DriftStateRecorded = true
	}
	return result, nil
}

// ExecuteStream preserves the same identity, drift and financial gates as
// Execute, but releases each complete non-terminal SSE frame as it arrives.
// The protocol terminal frame is held until the Core has settled and the
// Ledger projection has been published.
func (a *Adapter) ExecuteStream(
	ctx context.Context,
	request Request,
	onChunk func(CoreStreamChunk) error,
) (Result, error) {
	if onChunk == nil {
		return Result{}, ErrInvalidRequest
	}
	if err := validateRequest(request); err != nil {
		return Result{}, err
	}

	principal, err := a.identity.AuthenticateAPIKey(ctx, a.fingerprint(request.RawAPIKey))
	if err != nil {
		return Result{}, ErrUnauthorized
	}
	if principal.KeyRevoked {
		return Result{}, ErrAPIKeyRevoked
	}
	if !principal.UserEnabled {
		return Result{}, ErrUserDisabled
	}
	if strings.TrimSpace(principal.UserID) == "" || strings.TrimSpace(principal.APICredentialID) == "" {
		return Result{}, ErrUnauthorized
	}
	if a.emergencyGate.blocked(principal.UserID) {
		return Result{}, ErrProjectionDriftActive
	}
	allowed, err := a.driftGate.AllowFinancialWrite(ctx, principal.UserID)
	if err != nil {
		return Result{}, ErrDriftStateUnavailable
	}
	if !allowed {
		return Result{}, ErrProjectionDriftActive
	}
	mapping, err := a.models.ResolveModel(ctx, principal.Group, request.RequestedModel)
	if err != nil || strings.TrimSpace(mapping.CoreModel) == "" {
		return Result{}, ErrModelMappingMissing
	}
	streamingCore, ok := a.core.(StreamingCommercialCore)
	if !ok {
		return Result{}, ErrCoreExecutionFailed
	}

	terminal := make([]CoreStreamChunk, 0, 1)
	coreResult, err := streamingCore.ExecuteStream(ctx, CoreRequest{
		UserReference:         principal.UserID,
		KeyReference:          principal.APICredentialID,
		IdempotencyKey:        request.IdempotencyKey,
		RequestHash:           canonicalRequestHash(principal, mapping.CoreModel, request.Payload),
		Model:                 mapping.CoreModel,
		Payload:               append([]byte(nil), request.Payload...),
		FailurePlan:           cloneFailurePlan(request.FailurePlan),
		ClientResponseDelayMS: request.ClientResponseDelayMS,
	}, func(chunk CoreStreamChunk) error {
		chunk.Frame = append([]byte(nil), chunk.Frame...)
		if chunk.Terminal {
			terminal = append(terminal, chunk)
			return nil
		}
		return onChunk(chunk)
	})
	if err != nil {
		return Result{}, ErrCoreExecutionFailed
	}
	if err := validateCoreResult(coreResult); err != nil {
		return Result{}, err
	}

	projection := Projection{
		AuthoritativeRequestID: coreResult.AuthoritativeRequestID,
		LedgerReference:        coreResult.LedgerReference,
		UserReference:          principal.UserID,
		KeyReference:           principal.APICredentialID,
		Model:                  mapping.CoreModel,
		Status:                 coreResult.Status,
		InputTokens:            coreResult.InputTokens,
		OutputTokens:           coreResult.OutputTokens,
		CustomerChargeMicro:    coreResult.CustomerChargeMicro,
		ProviderCostMicro:      coreResult.ProviderCostMicro,
		BalanceAfterMicro:      coreResult.BalanceAfterMicro,
		RefundMicro:            coreResult.RefundMicro,
		NetRevenueMicro:        coreResult.NetRevenueMicro,
		Replay:                 coreResult.Replay,
	}
	result := Result{
		Core:                 cloneCoreResult(coreResult),
		FinanciallyCommitted: coreResult.Status == OutcomeSettled,
	}
	if err := a.projections.Publish(ctx, projection); err != nil {
		result.ProjectionDrift = true
		a.emergencyGate.block(principal.UserID)
		recordCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), postSettlementDriftTimeout)
		defer cancel()
		if recordErr := a.driftGate.RecordProjectionDrift(recordCtx, projection, err); recordErr == nil {
			result.DriftStateRecorded = true
		}
	}
	for _, chunk := range terminal {
		if err := onChunk(chunk); err != nil {
			return result, fmt.Errorf("flush terminal stream frame: %w", err)
		}
	}
	return result, nil
}

// ReconcileProjection is the only operation that clears a drift gate. The
// caller must supply a freshly fetched Commercial Core projection; the gate
// implementation atomically replaces the Sub2API view and clears the block.
func (a *Adapter) ReconcileProjection(ctx context.Context, projection Projection) error {
	if strings.TrimSpace(projection.UserReference) == "" ||
		strings.TrimSpace(projection.AuthoritativeRequestID) == "" ||
		strings.TrimSpace(projection.LedgerReference) == "" ||
		projection.BalanceAfterMicro < 0 || projection.RefundMicro < 0 ||
		projection.NetRevenueMicro < 0 {
		return ErrInvalidRequest
	}
	if err := a.driftGate.ReconcileProjection(ctx, projection); err != nil {
		return ErrDriftStateUnavailable
	}
	a.emergencyGate.Clear(projection.UserReference)
	return nil
}

func validateRequest(request Request) error {
	if strings.TrimSpace(request.RawAPIKey) == "" ||
		strings.TrimSpace(request.IdempotencyKey) == "" ||
		strings.TrimSpace(request.RequestedModel) == "" {
		return ErrInvalidRequest
	}
	if len(request.IdempotencyKey) > 256 {
		return ErrInvalidRequest
	}
	return nil
}

func canonicalRequestHash(principal Principal, coreModel string, payload []byte) string {
	digest := sha256.New()
	writeHashField(digest, []byte("ai16t-sub2api-request-v1"))
	writeHashField(digest, []byte(principal.UserID))
	writeHashField(digest, []byte(coreModel))
	writeHashField(digest, payload)
	return hex.EncodeToString(digest.Sum(nil))
}

type hashWriter interface {
	Write([]byte) (int, error)
}

func writeHashField(writer hashWriter, value []byte) {
	var length [8]byte
	binary.BigEndian.PutUint64(length[:], uint64(len(value)))
	_, _ = writer.Write(length[:])
	_, _ = writer.Write(value)
}

func validateCoreResult(result CoreResult) error {
	if strings.TrimSpace(result.AuthoritativeRequestID) == "" {
		return ErrInvalidAuthorityResult
	}
	if result.InputTokens < 0 || result.OutputTokens < 0 ||
		result.CustomerChargeMicro < 0 || result.ProviderCostMicro < 0 ||
		result.BalanceAfterMicro < 0 || result.RefundMicro < 0 ||
		result.NetRevenueMicro < 0 || result.RefundMicro > result.CustomerChargeMicro ||
		result.NetRevenueMicro != result.CustomerChargeMicro-result.RefundMicro {
		return ErrInvalidAuthorityResult
	}
	switch result.Status {
	case OutcomeSettled:
		if strings.TrimSpace(result.LedgerReference) == "" {
			return ErrInvalidAuthorityResult
		}
		if result.ContentType != "" && result.ContentType != "application/json" && result.ContentType != "text/event-stream" {
			return ErrInvalidAuthorityResult
		}
	case OutcomeFailed:
		if result.CustomerChargeMicro != 0 {
			return ErrInvalidAuthorityResult
		}
	default:
		return ErrInvalidAuthorityResult
	}
	return nil
}

func (a *Adapter) fingerprint(rawAPIKey string) string {
	mac := hmac.New(sha256.New, a.fingerprintKey)
	_, _ = mac.Write([]byte(rawAPIKey))
	return hex.EncodeToString(mac.Sum(nil))
}

func cloneCoreResult(result CoreResult) CoreResult {
	result.Response = append([]byte(nil), result.Response...)
	return result
}

func cloneFailurePlan(source map[string]string) map[string]string {
	if len(source) == 0 {
		return nil
	}
	result := make(map[string]string, len(source))
	for key, value := range source {
		result[key] = value
	}
	return result
}
