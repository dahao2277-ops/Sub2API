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
	result  CoreResult
	err     error
	request CoreRequest
	calls   int
}

func (s *coreStub) Execute(_ context.Context, request CoreRequest) (CoreResult, error) {
	s.calls++
	s.request = request
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
	calls int
	cause error
}

func (s *driftStub) ReportProjectionDrift(_ context.Context, _ Projection, cause error) {
	s.calls++
	s.cause = cause
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
	drift := &driftStub{}
	adapter, err := New(identity, models, core, projections, drift, []byte(strings.Repeat("k", 32)))
	require.NoError(t, err)
	return adapter, identity, core, projections, drift
}

func validRequest() Request {
	return Request{
		RawAPIKey:      "sub2api-test-secret",
		IdempotencyKey: "idem-1",
		RequestedModel: "public-model",
		RequestHash:    "sha256:request",
		Payload:        []byte(`{"messages":[]}`),
	}
}

func TestExecutePassesOnlyReferencesToCommercialCore(t *testing.T) {
	adapter, identity, core, projections, _ := validFixture(t)
	request := validRequest()

	result, err := adapter.Execute(context.Background(), request)
	require.NoError(t, err)
	require.False(t, result.ProjectionDrift)
	require.NotEqual(t, request.RawAPIKey, identity.fingerprint)
	require.Len(t, identity.fingerprint, 64)
	require.NotContains(t, core.request.Payload, request.RawAPIKey)
	require.Equal(t, "user-1", core.request.UserReference)
	require.Equal(t, "key-1", core.request.KeyReference)
	require.Equal(t, request.IdempotencyKey, core.request.IdempotencyKey)
	require.Equal(t, request.RequestHash, core.request.RequestHash)
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

func TestExecuteReportsProjectionDriftWithoutRebilling(t *testing.T) {
	adapter, _, core, projections, drift := validFixture(t)
	projectionErr := errors.New("projection database unavailable")
	projections.err = projectionErr

	result, err := adapter.Execute(context.Background(), validRequest())
	require.NoError(t, err)
	require.True(t, result.ProjectionDrift)
	require.Equal(t, 1, core.calls)
	require.Equal(t, 1, projections.calls)
	require.Equal(t, 1, drift.calls)
	require.ErrorIs(t, drift.cause, projectionErr)
}

func TestNewRequiresStrongFingerprintKey(t *testing.T) {
	_, err := New(&identityStub{}, &modelStub{}, &coreStub{}, &projectionStub{}, &driftStub{}, []byte("short"))
	require.ErrorIs(t, err, ErrInvalidRequest)
}
