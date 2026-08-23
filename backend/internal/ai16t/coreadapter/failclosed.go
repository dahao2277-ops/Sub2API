package coreadapter

import "context"

type FailClosedAdapter struct{}

func NewFailClosedAdapter() *FailClosedAdapter {
	return &FailClosedAdapter{}
}

func (a *FailClosedAdapter) Quote(context.Context, QuoteRequest) (*QuoteResult, error) {
	return nil, ErrCoreUnavailable
}

func (a *FailClosedAdapter) Reserve(context.Context, ReserveRequest) (*ReserveResult, error) {
	return nil, ErrCoreUnavailable
}

func (a *FailClosedAdapter) Settle(context.Context, SettleRequest) (*SettleResult, error) {
	return nil, ErrCoreUnavailable
}

func (a *FailClosedAdapter) Refund(context.Context, RefundRequest) (*RefundResult, error) {
	return nil, ErrCoreUnavailable
}

func (a *FailClosedAdapter) CheckProjection(context.Context, BalanceProjection) error {
	return ErrCoreUnavailable
}

type DisabledSecretProvider struct{}

func NewDisabledSecretProvider() *DisabledSecretProvider {
	return &DisabledSecretProvider{}
}

func (p *DisabledSecretProvider) Get(context.Context, SecretRef) (*SecretMaterial, error) {
	return nil, ErrSecretUnavailable
}

func (p *DisabledSecretProvider) Put(context.Context, SecretMaterial) (*SecretRef, error) {
	return nil, ErrSecretWriteDisabled
}

func (p *DisabledSecretProvider) Rotate(context.Context, SecretRef) (*SecretRef, error) {
	return nil, ErrSecretWriteDisabled
}
