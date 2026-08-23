package ai16t

import (
	"context"
	"errors"
	"testing"
)

type fakeLedger struct {
	reserveResult ReserveResult
	settleResult  UsageResult
	refundResult  UsageResult
	err           error
}

func (f fakeLedger) Reserve(context.Context, UsageRequest) (ReserveResult, error) {
	return f.reserveResult, f.err
}

func (f fakeLedger) Settle(context.Context, SettleRequest) (UsageResult, error) {
	return f.settleResult, f.err
}

func (f fakeLedger) Refund(context.Context, string, string) (UsageResult, error) {
	return f.refundResult, f.err
}

type fakeProjection struct {
	balance string
	written ProjectionResult
	err     error
}

func (f *fakeProjection) ReadBalanceUSD(context.Context, string) (string, error) {
	return f.balance, f.err
}

func (f *fakeProjection) WriteProjection(_ context.Context, _ string, result ProjectionResult) error {
	f.written = result
	return nil
}

func TestNewAdapterRequiresLedger(t *testing.T) {
	_, err := NewAdapter(nil, nil, nil)
	if !errors.Is(err, ErrCoreUnavailable) {
		t.Fatalf("expected ErrCoreUnavailable, got %v", err)
	}
}

func TestReserveRequiresIdempotencyKey(t *testing.T) {
	adapter, err := NewAdapter(fakeLedger{}, nil, nil)
	if err != nil {
		t.Fatalf("new adapter: %v", err)
	}

	_, err = adapter.Reserve(context.Background(), UsageRequest{AccountID: "acct_1"})
	if !errors.Is(err, ErrMissingIdempotencyKey) {
		t.Fatalf("expected ErrMissingIdempotencyKey, got %v", err)
	}
}

func TestSettleReportsProjectionDriftWhenLedgerWins(t *testing.T) {
	projection := &fakeProjection{balance: "9.00"}
	adapter, err := NewAdapter(fakeLedger{settleResult: UsageResult{
		Decision:        LedgerDecisionSettled,
		BalanceAfterUSD: "8.50",
	}}, nil, projection)
	if err != nil {
		t.Fatalf("new adapter: %v", err)
	}

	result, err := adapter.Settle(context.Background(), SettleRequest{
		ReservationID: "rsv_1",
		Usage: UsageRequest{
			AccountID:      "acct_1",
			IdempotencyKey: "idem_1",
		},
	})
	if err != nil {
		t.Fatalf("settle: %v", err)
	}
	if result.Projection.Drift != DriftStatusProjectionDrift {
		t.Fatalf("expected drift, got %s", result.Projection.Drift)
	}
	if projection.written.Drift != DriftStatusProjectionDrift {
		t.Fatalf("expected written drift, got %s", projection.written.Drift)
	}
}
