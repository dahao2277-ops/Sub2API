package ai16tadapter

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

type identityStub struct {
	principal   Principal
	err         error
	fingerprint string
}

func (s *identityStub) AuthenticateAPIKey(_ context.Context, fingerprint string) (Principal, error) {
	s.fingerprint = fingerprint
	return s.principal, s.err
}

type modelStub struct {
	mapping ModelMapping
	err     error
}

func (s *modelStub) ResolveModel(_ context.Context, _, _ string) (ModelMapping, error) {
	return s.mapping, s.err
}

type coreStub struct {
	result   CoreResult
	err      error
	request  CoreRequest
	requests []CoreRequest
	calls    int
}

func (s *coreStub) Execute(_ context.Context, request CoreRequest) (CoreResult, error) {
	s.calls++
	s.request = request
	s.requests = append(s.requests, request)
	return s.result, s.err
}

type projectionStub struct {
	err        error
	projection Projection
	calls      int
}

func (s *projectionStub) Publish(_ context.Context, projection Projection) error {
	s.calls++
	s.projection = projection
	return s.err
}

type driftStub struct {
	allowed      bool
	allowErr     error
	allowCalls   int
	recordErr    error
	recordCalls  int
	recordCtxErr error
	clearErr     error
	clearCalls   int
	cause        error
}

func (s *driftStub) AllowFinancialWrite(_ context.Context, _ string) (bool, error) {
	s.allowCalls++
	return s.allowed, s.allowErr
}

func (s *driftStub) RecordProjectionDrift(ctx context.Context, _ Projection, cause error) error {
	return s.recordProjectionDrift(ctx, cause)
}

func (s *driftStub) recordProjectionDrift(ctx context.Context, cause error) error {
	s.recordCalls++
	s.recordCtxErr = ctx.Err()
	s.cause = cause
	return s.recordErr
}

func (s *driftStub) ClearProjectionDrift(_ context.Context, _ string) error {
	s.clearCalls++
	return s.clearErr
}

func validFixture(t *testing.T) (*Adapter, *identityStub, *coreStub, *projectionStub, *driftStub) {
	t.Helper()
	identity := &identityStub{principal: Principal{
		UserID: "user-1", APICredentialID: "key-1", Group: "default", UserEnabled: true,
	}}
	models := &modelStub{mapping: ModelMapping{PublicModel: "public-model", CoreModel: "core-model"}}
	core := &coreStub{result: CoreResult{
		AuthoritativeRequestID: "request-1",
		LedgerReference:        "ledger-1",
		Status:                 OutcomeSettled,
		Response:               []byte(`{"ok":true}`),
		InputTokens:            10,
		OutputTokens:           5,
		CustomerChargeMicro:    150,
		ProviderCostMicro:      100,
		BalanceAfterMicro:      850,
	}}
	projections := &projectionStub{}
	drift := &driftStub{allowed: true}
	adapter, err := New(identity, models, core, projections, drift, []byte(strings.Repeat("k", 32)))
	require.NoError(t, err)
	return adapter, identity, core, projections, drift
}

func validRequest() Request {
	return Request{
		RawAPIKey:      "sub2api-test-secret",
		IdempotencyKey: "idem-1",
		RequestedModel: "public-model",
		Payload:        []byte(`{"messages":[]}`),
	}
}

func TestExecutePassesOnlyReferencesToCommercialCore(t *testing.T) {
	adapter, identity, core, projections, _ := validFixture(t)
	request := validRequest()

	result, err := adapter.Execute(context.Background(), request)
	require.NoError(t, err)
	require.True(t, result.FinanciallyCommitted)
	require.False(t, result.ProjectionDrift)
	require.NotEqual(t, request.RawAPIKey, identity.fingerprint)
	require.Len(t, identity.fingerprint, 64)
	require.NotContains(t, core.request.Payload, request.RawAPIKey)
	require.Equal(t, "user-1", core.request.UserReference)
	require.Equal(t, "key-1", core.request.KeyReference)
	require.Equal(t, request.IdempotencyKey, core.request.IdempotencyKey)
	require.Equal(t, canonicalRequestHash(identity.principal, "core-model", request.Payload), core.request.RequestHash)
	require.Len(t, core.request.RequestHash, 64)
	require.Equal(t, "ledger-1", projections.projection.LedgerReference)
	require.Equal(t, int64(850), projections.projection.BalanceAfterMicro)
}

func TestExecuteBlocksRevokedKeyBeforeCore(t *testing.T) {
	adapter, identity, core, projections, _ := validFixture(t)
	identity.principal.KeyRevoked = true

	_, err := adapter.Execute(context.Background(), validRequest())
	require.ErrorIs(t, err, ErrAPIKeyRevoked)
	require.Zero(t, core.calls)
	require.Zero(t, projections.calls)
}

func TestExecuteBlocksDisabledUserBeforeCore(t *testing.T) {
	adapter, identity, core, projections, _ := validFixture(t)
	identity.principal.UserEnabled = false

	_, err := adapter.Execute(context.Background(), validRequest())
	require.ErrorIs(t, err, ErrUserDisabled)
	require.Zero(t, core.calls)
	require.Zero(t, projections.calls)
}

func TestExecuteRejectsSettledResultWithoutLedgerReference(t *testing.T) {
	adapter, _, core, projections, _ := validFixture(t)
	core.result.LedgerReference = ""

	_, err := adapter.Execute(context.Background(), validRequest())
	require.ErrorIs(t, err, ErrInvalidAuthorityResult)
	require.Zero(t, projections.calls)
}

func TestExecuteRejectsChargeOnFailedRequest(t *testing.T) {
	adapter, _, core, projections, _ := validFixture(t)
	core.result.Status = OutcomeFailed
	core.result.CustomerChargeMicro = 1
	core.result.LedgerReference = ""

	_, err := adapter.Execute(context.Background(), validRequest())
	require.ErrorIs(t, err, ErrInvalidAuthorityResult)
	require.Zero(t, projections.calls)
}

func TestExecuteRejectsNegativeTokenCounts(t *testing.T) {
	adapter, _, core, projections, _ := validFixture(t)
	core.result.InputTokens = -1

	_, err := adapter.Execute(context.Background(), validRequest())
	require.ErrorIs(t, err, ErrInvalidAuthorityResult)
	require.Zero(t, projections.calls)
}

func TestExecuteReportsProjectionDriftWithoutRebilling(t *testing.T) {
	adapter, _, core, projections, drift := validFixture(t)
	projectionErr := errors.New("projection database unavailable")
	projections.err = projectionErr

	result, err := adapter.Execute(context.Background(), validRequest())
	require.NoError(t, err)
	require.True(t, result.ProjectionDrift)
	require.True(t, result.FinanciallyCommitted)
	require.True(t, result.DriftStateRecorded)
	require.Equal(t, 1, core.calls)
	require.Equal(t, 1, projections.calls)
	require.Equal(t, 1, drift.recordCalls)
	require.ErrorIs(t, drift.cause, projectionErr)
}

func TestExecuteBlocksFinancialWriteWhileProjectionDriftIsActive(t *testing.T) {
	adapter, _, core, projections, drift := validFixture(t)
	drift.allowed = false

	_, err := adapter.Execute(context.Background(), validRequest())
	require.ErrorIs(t, err, ErrProjectionDriftActive)
	require.Zero(t, core.calls)
	require.Zero(t, projections.calls)
}

func TestExecuteFailsClosedWhenDriftStateIsUnavailable(t *testing.T) {
	adapter, _, core, projections, drift := validFixture(t)
	drift.allowErr = errors.New("drift database unavailable")

	_, err := adapter.Execute(context.Background(), validRequest())
	require.ErrorIs(t, err, ErrDriftStateUnavailable)
	require.Zero(t, core.calls)
	require.Zero(t, projections.calls)
}

func TestExecuteReturnsStablePublicErrors(t *testing.T) {
	adapter, identity, core, _, _ := validFixture(t)
	identity.err = errors.New("internal identity detail")

	_, err := adapter.Execute(context.Background(), validRequest())
	require.ErrorIs(t, err, ErrUnauthorized)
	require.NotContains(t, err.Error(), "internal identity detail")

	identity.err = nil
	core.err = errors.New("internal core detail")
	_, err = adapter.Execute(context.Background(), validRequest())
	require.ErrorIs(t, err, ErrCoreExecutionFailed)
	require.NotContains(t, err.Error(), "internal core detail")
}

func TestCanonicalRequestHashBindsIdentityModelAndPayload(t *testing.T) {
	principal := Principal{UserID: "user-1", APICredentialID: "key-1"}
	base := canonicalRequestHash(principal, "model-a", []byte("payload-a"))
	require.Equal(t, base, canonicalRequestHash(principal, "model-a", []byte("payload-a")))
	require.NotEqual(t, base, canonicalRequestHash(principal, "model-a", []byte("payload-b")))
	require.NotEqual(t, base, canonicalRequestHash(principal, "model-b", []byte("payload-a")))
	require.NotEqual(t, base, canonicalRequestHash(Principal{UserID: "user-2", APICredentialID: "key-1"}, "model-a", []byte("payload-a")))
}

func TestExecuteFailsClosedWhenDriftCannotBeRecorded(t *testing.T) {
	adapter, _, core, projections, drift := validFixture(t)
	projections.err = errors.New("projection unavailable")
	drift.recordErr = errors.New("drift store unavailable")

	result, err := adapter.Execute(context.Background(), validRequest())
	require.NoError(t, err)
	require.True(t, result.FinanciallyCommitted)
	require.True(t, result.ProjectionDrift)
	require.False(t, result.DriftStateRecorded)
	require.Equal(t, 1, core.calls)

	_, err = adapter.Execute(context.Background(), validRequest())
	require.ErrorIs(t, err, ErrProjectionDriftActive)
	require.Equal(t, 1, core.calls)
}

func TestProjectionDriftRecordSurvivesCanceledRequestContext(t *testing.T) {
	adapter, _, _, projections, drift := validFixture(t)
	projections.err = errors.New("projection unavailable")
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	result, err := adapter.Execute(ctx, validRequest())
	require.NoError(t, err)
	require.True(t, result.FinanciallyCommitted)
	require.True(t, result.DriftStateRecorded)
	require.NoError(t, drift.recordCtxErr)
}

func TestClearProjectionDriftRequiresExplicitReconciliation(t *testing.T) {
	adapter, _, core, projections, drift := validFixture(t)
	projections.err = errors.New("projection unavailable")

	_, err := adapter.Execute(context.Background(), validRequest())
	require.NoError(t, err)
	_, err = adapter.Execute(context.Background(), validRequest())
	require.ErrorIs(t, err, ErrProjectionDriftActive)
	require.Equal(t, 1, core.calls)

	require.NoError(t, adapter.ClearProjectionDrift(context.Background(), "user-1"))
	require.Equal(t, 1, drift.clearCalls)
	_, err = adapter.Execute(context.Background(), validRequest())
	require.NoError(t, err)
	require.Equal(t, 2, core.calls)
}

func TestNewRequiresStrongFingerprintKey(t *testing.T) {
	_, err := New(&identityStub{}, &modelStub{}, &coreStub{}, &projectionStub{}, &driftStub{allowed: true}, []byte("short"))
	require.ErrorIs(t, err, ErrInvalidRequest)
}
