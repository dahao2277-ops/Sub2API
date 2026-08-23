package ai16tadapter

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"
)

const minimumFingerprintKeyBytes = 32

type Adapter struct {
	identity       IdentitySource
	models         ModelSource
	core           CommercialCore
	projections    ProjectionSink
	driftReporter  DriftReporter
	fingerprintKey []byte
}

func New(
	identity IdentitySource,
	models ModelSource,
	core CommercialCore,
	projections ProjectionSink,
	driftReporter DriftReporter,
	fingerprintKey []byte,
) (*Adapter, error) {
	if identity == nil || models == nil || core == nil || projections == nil || driftReporter == nil {
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
		driftReporter:  driftReporter,
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
		return Result{}, fmt.Errorf("%w: %v", ErrUnauthorized, err)
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

	mapping, err := a.models.ResolveModel(ctx, principal.Group, request.RequestedModel)
	if err != nil || strings.TrimSpace(mapping.CoreModel) == "" {
		return Result{}, ErrModelMappingMissing
	}

	coreResult, err := a.core.Execute(ctx, CoreRequest{
		UserReference:  principal.UserID,
		KeyReference:   principal.APICredentialID,
		IdempotencyKey: request.IdempotencyKey,
		RequestHash:    request.RequestHash,
		Model:          mapping.CoreModel,
		Payload:        append([]byte(nil), request.Payload...),
	})
	if err != nil {
		return Result{}, err
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
		Replay:                 coreResult.Replay,
	}

	result := Result{Core: cloneCoreResult(coreResult)}
	if err := a.projections.Publish(ctx, projection); err != nil {
		result.ProjectionDrift = true
		a.driftReporter.ReportProjectionDrift(ctx, projection, err)
	}
	return result, nil
}

func validateRequest(request Request) error {
	if strings.TrimSpace(request.RawAPIKey) == "" ||
		strings.TrimSpace(request.IdempotencyKey) == "" ||
		strings.TrimSpace(request.RequestHash) == "" ||
		strings.TrimSpace(request.RequestedModel) == "" {
		return ErrInvalidRequest
	}
	if len(request.IdempotencyKey) > 256 || len(request.RequestHash) > 256 {
		return ErrInvalidRequest
	}
	return nil
}

func validateCoreResult(result CoreResult) error {
	if strings.TrimSpace(result.AuthoritativeRequestID) == "" {
		return ErrInvalidAuthorityResult
	}
	if result.CustomerChargeMicro < 0 || result.ProviderCostMicro < 0 || result.BalanceAfterMicro < 0 {
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
