package coreadapter

import (
	"context"
	"errors"
	"math"
	"testing"
	"time"
)

func TestFailClosedAdapterRejectsFinancialOperations(t *testing.T) {
	adapter := NewFailClosedAdapter()
	ctx := context.Background()

	if _, err := adapter.Quote(ctx, QuoteRequest{}); !errors.Is(err, ErrCoreUnavailable) {
		t.Fatalf("Quote error = %v, want %v", err, ErrCoreUnavailable)
	}
	if _, err := adapter.Reserve(ctx, ReserveRequest{}); !errors.Is(err, ErrCoreUnavailable) {
		t.Fatalf("Reserve error = %v, want %v", err, ErrCoreUnavailable)
	}
	if _, err := adapter.Settle(ctx, SettleRequest{}); !errors.Is(err, ErrCoreUnavailable) {
		t.Fatalf("Settle error = %v, want %v", err, ErrCoreUnavailable)
	}
	if _, err := adapter.Refund(ctx, RefundRequest{}); !errors.Is(err, ErrCoreUnavailable) {
		t.Fatalf("Refund error = %v, want %v", err, ErrCoreUnavailable)
	}
}

func TestProjectionDriftDetectionDoesNotOverflow(t *testing.T) {
	projection := BalanceProjection{
		Sub2APIProjection:   AmountMicros(math.MaxInt64),
		LedgerAuthoritative: AmountMicros(math.MinInt64),
		AllowedDriftMicros:  AmountMicros(math.MaxInt64),
	}
	if err := CheckProjectionDrift(context.Background(), projection); !errors.Is(err, ErrProjectionDrift) {
		t.Fatalf("expected overflow-safe drift result, got %v", err)
	}

	projection.AllowedDriftMicros = -1
	if err := CheckProjectionDrift(context.Background(), projection); !errors.Is(err, ErrProjectionDrift) {
		t.Fatalf("expected negative tolerance to fail closed, got %v", err)
	}
}

func TestProjectionDriftDetection(t *testing.T) {
	ctx := context.Background()
	projection := BalanceProjection{
		UserID:              1001,
		Sub2APIProjection:   1_000_000,
		LedgerAuthoritative: 999_500,
		AllowedDriftMicros:  1_000,
		CheckedAt:           time.Now(),
	}
	if err := CheckProjectionDrift(ctx, projection); err != nil {
		t.Fatalf("expected projection inside tolerance, got %v", err)
	}

	projection.LedgerAuthoritative = 998_000
	if err := CheckProjectionDrift(ctx, projection); !errors.Is(err, ErrProjectionDrift) {
		t.Fatalf("expected projection drift, got %v", err)
	}
}

func TestDisabledSecretProviderDoesNotExposeOrPersistSecrets(t *testing.T) {
	provider := NewDisabledSecretProvider()
	ctx := context.Background()

	if _, err := provider.Get(ctx, SecretRef{Provider: "mock", AccountID: 1}); !errors.Is(err, ErrSecretUnavailable) {
		t.Fatalf("Get error = %v, want %v", err, ErrSecretUnavailable)
	}
	if _, err := provider.Put(ctx, SecretMaterial{Value: "test-secret"}); !errors.Is(err, ErrSecretWriteDisabled) {
		t.Fatalf("Put error = %v, want %v", err, ErrSecretWriteDisabled)
	}
	if _, err := provider.Rotate(ctx, SecretRef{Provider: "mock", AccountID: 1}); !errors.Is(err, ErrSecretWriteDisabled) {
		t.Fatalf("Rotate error = %v, want %v", err, ErrSecretWriteDisabled)
	}
	if err := provider.Delete(ctx, SecretRef{Provider: "mock", AccountID: 1}); !errors.Is(err, ErrSecretWriteDisabled) {
		t.Fatalf("Delete error = %v, want %v", err, ErrSecretWriteDisabled)
	}
}
