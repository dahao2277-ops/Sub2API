package coreadapter

import (
	"context"
	"errors"
	"time"
)

var (
	ErrCoreUnavailable     = errors.New("AI16T_CORE_UNAVAILABLE")
	ErrLedgerRejected      = errors.New("AI16T_LEDGER_REJECTED")
	ErrProjectionDrift     = errors.New("PROJECTION_DRIFT")
	ErrSecretUnavailable   = errors.New("SECRET_UNAVAILABLE")
	ErrSecretWriteDisabled = errors.New("SECRET_WRITE_DISABLED")
)

type AmountMicros int64

type RequestContext struct {
	Platform        string
	UserID          int64
	APIKeyID        int64
	AccountID       int64
	RequestID       string
	IdempotencyKey  string
	Model           string
	Route           string
	StartedAt       time.Time
	ClientRequestID string
}

type Usage struct {
	InputTokens           int
	OutputTokens          int
	CacheCreationTokens   int
	CacheReadTokens       int
	CacheCreation5mTokens int
	CacheCreation1hTokens int
	DurationMs            *int
}

type QuoteRequest struct {
	Context RequestContext
	Usage   Usage
}

type QuoteResult struct {
	ProviderCostMicros AmountMicros
	CustomerCostMicros AmountMicros
	MinimumMarginBps   int64
	PricingVersion     string
}

type ReserveRequest struct {
	Context            RequestContext
	MaxChargeMicros    AmountMicros
	RequestFingerprint string
}

type ReserveResult struct {
	ReservationID        string
	AuthoritativeBalance AmountMicros
	ProjectedBalance     AmountMicros
	IdempotencyReplay    bool
	LedgerTransactionID  string
}

type SettleRequest struct {
	Context       RequestContext
	ReservationID string
	Usage         Usage
	Quote         QuoteResult
	FinalStatus   string
}

type SettleResult struct {
	LedgerTransactionID  string
	CustomerChargeMicros AmountMicros
	ProviderCostMicros   AmountMicros
	AuthoritativeBalance AmountMicros
	IdempotencyReplay    bool
}

type RefundRequest struct {
	Context             RequestContext
	ReservationID       string
	LedgerTransactionID string
	Reason              string
}

type RefundResult struct {
	LedgerTransactionID  string
	RefundedMicros       AmountMicros
	AuthoritativeBalance AmountMicros
	IdempotencyReplay    bool
}

type BalanceProjection struct {
	UserID              int64
	Sub2APIProjection   AmountMicros
	LedgerAuthoritative AmountMicros
	AllowedDriftMicros  AmountMicros
	CheckedAt           time.Time
}

type LedgerClient interface {
	Quote(ctx context.Context, req QuoteRequest) (*QuoteResult, error)
	Reserve(ctx context.Context, req ReserveRequest) (*ReserveResult, error)
	Settle(ctx context.Context, req SettleRequest) (*SettleResult, error)
	Refund(ctx context.Context, req RefundRequest) (*RefundResult, error)
}

type SecretRef struct {
	Provider  string
	AccountID int64
	Version   string
}

type SecretMaterial struct {
	Ref       SecretRef
	Value     string
	ExpiresAt *time.Time
}

type SecretProvider interface {
	Get(ctx context.Context, ref SecretRef) (*SecretMaterial, error)
	Put(ctx context.Context, material SecretMaterial) (*SecretRef, error)
	Rotate(ctx context.Context, ref SecretRef) (*SecretRef, error)
}

type Adapter interface {
	Quote(ctx context.Context, req QuoteRequest) (*QuoteResult, error)
	Reserve(ctx context.Context, req ReserveRequest) (*ReserveResult, error)
	Settle(ctx context.Context, req SettleRequest) (*SettleResult, error)
	Refund(ctx context.Context, req RefundRequest) (*RefundResult, error)
	CheckProjection(ctx context.Context, projection BalanceProjection) error
}
