package coreadapter

import (
	"context"
	"math/big"
)

func CheckProjectionDrift(_ context.Context, projection BalanceProjection) error {
	if projection.AllowedDriftMicros < 0 {
		return ErrProjectionDrift
	}
	delta := new(big.Int).Sub(
		big.NewInt(int64(projection.Sub2APIProjection)),
		big.NewInt(int64(projection.LedgerAuthoritative)),
	)
	delta.Abs(delta)
	if delta.Cmp(big.NewInt(int64(projection.AllowedDriftMicros))) > 0 {
		return ErrProjectionDrift
	}
	return nil
}

type ProjectionCheckerAdapter struct {
	LedgerClient
}

func (a *ProjectionCheckerAdapter) CheckProjection(ctx context.Context, projection BalanceProjection) error {
	return CheckProjectionDrift(ctx, projection)
}
