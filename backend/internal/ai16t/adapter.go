package ai16t

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"
)

var (
	ErrMissingAccountID      = errors.New("ai16t: missing account id")
	ErrMissingIdempotencyKey = errors.New("ai16t: missing idempotency key")
	ErrCoreUnavailable       = errors.New("ai16t: commercial core unavailable")
	ErrLedgerRejected        = errors.New("ai16t: ledger rejected request")
)

type LedgerDecision string

const (
	LedgerDecisionReserved LedgerDecision = "reserved"
	LedgerDecisionSettled  LedgerDecision = "settled"
	LedgerDecisionRefunded LedgerDecision = "refunded"
	LedgerDecisionRejected LedgerDecision = "rejected"
)

type DriftStatus string

const (
	DriftStatusNone            DriftStatus = "NONE"
	DriftStatusProjectionDrift DriftStatus = "PROJECTION_DRIFT"
)

type SecretPurpose string

const (
	SecretPurposeProviderCredential SecretPurpose = "provider_credential"
	SecretPurposeWebhookSigning     SecretPurpose = "webhook_signing"
)

type UsageRequest struct {
	AccountID      string
	UserID         string
	APIKeyID       string
	Model          string
	IdempotencyKey string
	RequestID      string
	StartedAt      time.Time
	Metadata       map[string]string
}

type UsageResult struct {
	LedgerTransactionID string
	Decision            LedgerDecision
	CustomerChargeUSD   string
	ProviderCostUSD     string
	MarginUSD           string
	BalanceAfterUSD     string
	Replayed            bool
	Projection          ProjectionResult
}

type ProjectionResult struct {
	Sub2APIBalanceUSD string
	LedgerBalanceUSD  string
	Drift             DriftStatus
}

type ReserveResult struct {
	ReservationID string
	Replayed      bool
}

type SettleRequest struct {
	ReservationID   string
	Usage           UsageRequest
	InputTokens     int64
	OutputTokens    int64
	CustomerCharge  string
	ProviderCost    string
	ProviderRequest string
}

type LedgerClient interface {
	Reserve(ctx context.Context, request UsageRequest) (ReserveResult, error)
	Settle(ctx context.Context, request SettleRequest) (UsageResult, error)
	Refund(ctx context.Context, reservationID string, reason string) (UsageResult, error)
}

type SecretProvider interface {
	Resolve(ctx context.Context, purpose SecretPurpose, reference string) (string, error)
	Store(ctx context.Context, purpose SecretPurpose, plaintext string) (string, error)
	Rotate(ctx context.Context, purpose SecretPurpose, reference string) (string, error)
}

type ProjectionStore interface {
	ReadBalanceUSD(ctx context.Context, accountID string) (string, error)
	WriteProjection(ctx context.Context, accountID string, projection ProjectionResult) error
}

type Adapter struct {
	ledger     LedgerClient
	secrets    SecretProvider
	projection ProjectionStore
}

func NewAdapter(ledger LedgerClient, secrets SecretProvider, projection ProjectionStore) (*Adapter, error) {
	if ledger == nil {
		return nil, fmt.Errorf("%w: ledger client is required", ErrCoreUnavailable)
	}
	return &Adapter{ledger: ledger, secrets: secrets, projection: projection}, nil
}

func (a *Adapter) Reserve(ctx context.Context, request UsageRequest) (ReserveResult, error) {
	if err := validateUsageRequest(request); err != nil {
		return ReserveResult{}, err
	}
	result, err := a.ledger.Reserve(ctx, request)
	if err != nil {
		return ReserveResult{}, fmt.Errorf("%w: %v", ErrLedgerRejected, err)
	}
	return result, nil
}

func (a *Adapter) Settle(ctx context.Context, request SettleRequest) (UsageResult, error) {
	if err := validateUsageRequest(request.Usage); err != nil {
		return UsageResult{}, err
	}
	if strings.TrimSpace(request.ReservationID) == "" {
		return UsageResult{}, fmt.Errorf("%w: reservation id is required", ErrLedgerRejected)
	}
	result, err := a.ledger.Settle(ctx, request)
	if err != nil {
		return UsageResult{}, fmt.Errorf("%w: %v", ErrLedgerRejected, err)
	}
	if a.projection != nil {
		result.Projection = a.compareProjection(ctx, request.Usage.AccountID, result.BalanceAfterUSD)
	}
	return result, nil
}

func (a *Adapter) Refund(ctx context.Context, reservationID string, reason string) (UsageResult, error) {
	if strings.TrimSpace(reservationID) == "" {
		return UsageResult{}, fmt.Errorf("%w: reservation id is required", ErrLedgerRejected)
	}
	result, err := a.ledger.Refund(ctx, reservationID, reason)
	if err != nil {
		return UsageResult{}, fmt.Errorf("%w: %v", ErrLedgerRejected, err)
	}
	return result, nil
}

func (a *Adapter) ResolveProviderSecret(ctx context.Context, reference string) (string, error) {
	if a.secrets == nil {
		return "", fmt.Errorf("%w: secret provider is required", ErrCoreUnavailable)
	}
	return a.secrets.Resolve(ctx, SecretPurposeProviderCredential, reference)
}

func (a *Adapter) compareProjection(ctx context.Context, accountID string, ledgerBalance string) ProjectionResult {
	projection := ProjectionResult{LedgerBalanceUSD: ledgerBalance, Drift: DriftStatusNone}
	sub2apiBalance, err := a.projection.ReadBalanceUSD(ctx, accountID)
	if err != nil {
		projection.Drift = DriftStatusProjectionDrift
		_ = a.projection.WriteProjection(ctx, accountID, projection)
		return projection
	}
	projection.Sub2APIBalanceUSD = sub2apiBalance
	if strings.TrimSpace(sub2apiBalance) != strings.TrimSpace(ledgerBalance) {
		projection.Drift = DriftStatusProjectionDrift
	}
	_ = a.projection.WriteProjection(ctx, accountID, projection)
	return projection
}

func validateUsageRequest(request UsageRequest) error {
	if strings.TrimSpace(request.AccountID) == "" {
		return ErrMissingAccountID
	}
	if strings.TrimSpace(request.IdempotencyKey) == "" {
		return ErrMissingIdempotencyKey
	}
	return nil
}
