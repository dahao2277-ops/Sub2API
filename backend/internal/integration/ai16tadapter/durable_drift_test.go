package ai16tadapter

import (
	"context"
	"errors"
	"testing"
)

type driftGateFixture struct {
	allowed    bool
	recordErr  error
	reconciled bool
}

func (f *driftGateFixture) AllowFinancialWrite(context.Context, string) (bool, error) {
	return f.allowed, nil
}

func (f *driftGateFixture) RecordProjectionDrift(context.Context, Projection, error) error {
	return f.recordErr
}

func (f *driftGateFixture) ReconcileProjection(context.Context, Projection) error {
	f.reconciled = true
	f.allowed = true
	return nil
}

func TestDurableDriftMarkerSurvivesPrimaryWriteFailureAndRestart(t *testing.T) {
	directory := t.TempDir() + "/drift"
	primary := &driftGateFixture{allowed: true, recordErr: errors.New("redis unavailable")}
	gate, err := NewDurableDriftGate(primary, directory)
	if err != nil {
		t.Fatal(err)
	}
	projection := Projection{
		AuthoritativeRequestID: "req-1",
		LedgerReference:        "ledger-1",
		UserReference:          "user-1",
		BalanceAfterMicro:      100,
	}
	if err := gate.RecordProjectionDrift(context.Background(), projection, errors.New("publish")); err == nil {
		t.Fatal("primary failure was not returned")
	}

	restarted, err := NewDurableDriftGate(&driftGateFixture{allowed: true}, directory)
	if err != nil {
		t.Fatal(err)
	}
	allowed, err := restarted.AllowFinancialWrite(context.Background(), "user-1")
	if err != nil {
		t.Fatal(err)
	}
	if allowed {
		t.Fatal("durable marker did not survive restart")
	}
	if err := restarted.ReconcileProjection(context.Background(), projection); err != nil {
		t.Fatal(err)
	}
	allowed, err = restarted.AllowFinancialWrite(context.Background(), "user-1")
	if err != nil || !allowed {
		t.Fatalf("reconciliation did not clear marker: allowed=%v err=%v", allowed, err)
	}
}
