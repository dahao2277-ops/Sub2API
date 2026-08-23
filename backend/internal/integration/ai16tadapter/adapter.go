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
	driftedUsers   sync.Map
}

func New(
	identity IdentitySource,
	models ModelSource,
	core CommercialCore,
	projections ProjectionSink,
	driftGate DriftGate,
	fingerprintKey []byte,
) (*Adapter, error) {
	if identity == nil || models == nil || core == nil || projections == nil || driftGate == nil {
		return nil, fmt.Errorf("%w: dependencies are required", ErrInvalidRequest)
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
	if _, blocked := a.driftedUsers.Load(principal.UserID); blocked {
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
		a.driftedUsers.Store(principal.UserID, struct{}{})
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
	a.driftedUsers.Delete(projection.UserReference)
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
	writeHashField(digest, []byte(principal.APICredentialID))
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
