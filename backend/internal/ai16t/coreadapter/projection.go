package coreadapter

import "context"

func CheckProjectionDrift(_ context.Context, projection BalanceProjection) error {
	delta := projection.Sub2APIProjection - projection.LedgerAuthoritative
	if delta < 0 {
		delta = -delta
	}
	if delta > projection.AllowedDriftMicros {
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
