package ai16tadapter

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"golang.org/x/sys/unix"
)

type DurableDriftGate struct {
	primary   DriftGate
	directory string
	lockPath  string
}

func NewDurableDriftGate(primary DriftGate, directory string) (*DurableDriftGate, error) {
	if primary == nil || !filepath.IsAbs(directory) {
		return nil, ErrInvalidRequest
	}
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return nil, fmt.Errorf("create durable drift directory: %w", err)
	}
	if err := os.Chmod(directory, 0o700); err != nil {
		return nil, fmt.Errorf("secure durable drift directory: %w", err)
	}
	details, err := os.Lstat(directory)
	if err != nil || !details.IsDir() || details.Mode()&os.ModeSymlink != 0 || details.Mode().Perm() != 0o700 {
		return nil, ErrInvalidRequest
	}
	gate := &DurableDriftGate{
		primary:   primary,
		directory: directory,
		lockPath:  filepath.Join(directory, ".lock"),
	}
	if err := gate.withLock(func() error { return nil }); err != nil {
		return nil, err
	}
	return gate, nil
}

func (g *DurableDriftGate) AllowFinancialWrite(ctx context.Context, userReference string) (bool, error) {
	if strings.TrimSpace(userReference) == "" {
		return false, ErrInvalidRequest
	}
	allowed, err := g.primary.AllowFinancialWrite(ctx, userReference)
	if err != nil || !allowed {
		return false, err
	}
	blocked := true
	err = g.withLock(func() error {
		_, statErr := os.Lstat(g.markerPath(userReference))
		switch {
		case statErr == nil:
			blocked = true
			return nil
		case errors.Is(statErr, os.ErrNotExist):
			blocked = false
			return nil
		default:
			return statErr
		}
	})
	if err != nil {
		return false, err
	}
	return !blocked, nil
}

func (g *DurableDriftGate) RecordProjectionDrift(
	ctx context.Context,
	projection Projection,
	cause error,
) error {
	if strings.TrimSpace(projection.UserReference) == "" || strings.TrimSpace(projection.AuthoritativeRequestID) == "" {
		return ErrInvalidRequest
	}
	payload, err := json.Marshal(map[string]string{
		"authority":                "LEDGER_WINS",
		"authoritative_request_id": projection.AuthoritativeRequestID,
		"code":                     "PROJECTION_DRIFT",
		"recorded_at":              time.Now().UTC().Format(time.RFC3339Nano),
	})
	if err != nil {
		return err
	}
	if err := g.withLock(func() error {
		return g.atomicWrite(g.markerPath(projection.UserReference), payload)
	}); err != nil {
		return err
	}
	return g.primary.RecordProjectionDrift(ctx, projection, cause)
}

func (g *DurableDriftGate) ReconcileProjection(ctx context.Context, projection Projection) error {
	if err := g.primary.ReconcileProjection(ctx, projection); err != nil {
		return err
	}
	return g.withLock(func() error {
		err := os.Remove(g.markerPath(projection.UserReference))
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
		directory, err := os.Open(g.directory)
		if err != nil {
			return err
		}
		defer directory.Close()
		return directory.Sync()
	})
}

func (g *DurableDriftGate) markerPath(userReference string) string {
	digest := sha256.Sum256([]byte("ai16t-drift-v1\x00" + userReference))
	return filepath.Join(g.directory, hex.EncodeToString(digest[:])+".json")
}

func (g *DurableDriftGate) withLock(operation func() error) error {
	lock, err := os.OpenFile(g.lockPath, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return err
	}
	defer lock.Close()
	if err := lock.Chmod(0o600); err != nil {
		return err
	}
	if err := unix.Flock(int(lock.Fd()), unix.LOCK_EX); err != nil {
		return err
	}
	defer unix.Flock(int(lock.Fd()), unix.LOCK_UN) //nolint:errcheck
	return operation()
}

func (g *DurableDriftGate) atomicWrite(path string, payload []byte) error {
	temporary, err := os.CreateTemp(g.directory, ".drift-*")
	if err != nil {
		return err
	}
	temporaryName := temporary.Name()
	committed := false
	defer func() {
		_ = temporary.Close()
		if !committed {
			_ = os.Remove(temporaryName)
		}
	}()
	if err := temporary.Chmod(0o600); err != nil {
		return err
	}
	if _, err := temporary.Write(payload); err != nil {
		return err
	}
	if err := temporary.Sync(); err != nil {
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	if err := os.Rename(temporaryName, path); err != nil {
		return err
	}
	directory, err := os.Open(g.directory)
	if err != nil {
		return err
	}
	defer directory.Close()
	if err := directory.Sync(); err != nil {
		return err
	}
	committed = true
	return nil
}
