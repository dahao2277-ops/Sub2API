package ai16tsecret

import (
	"encoding/base64"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestEncryptedStoreLifecycle(t *testing.T) {
	directory := t.TempDir()
	keyPath := filepath.Join(directory, "master")
	key := base64.RawStdEncoding.EncodeToString([]byte("0123456789abcdef0123456789abcdef"))
	if err := os.WriteFile(keyPath, []byte(key), 0o600); err != nil {
		t.Fatal(err)
	}
	dataDir := filepath.Join(directory, "data")
	store, err := NewStore(dataDir, keyPath)
	if err != nil {
		t.Fatal(err)
	}
	secret := "sk-sensitive-value-never-persisted-in-plaintext"
	created, err := store.Put("apiyi/integration", secret)
	if err != nil {
		t.Fatal(err)
	}
	if created.Version != 1 || created.Last4 != "text" || len(created.Fingerprint) != 64 {
		t.Fatalf("unexpected metadata: %#v", created)
	}
	payload, err := os.ReadFile(filepath.Join(dataDir, storeFileName))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(payload), secret) {
		t.Fatal("encrypted store contains plaintext secret")
	}
	details, err := os.Stat(filepath.Join(dataDir, storeFileName))
	if err != nil {
		t.Fatal(err)
	}
	if details.Mode().Perm() != 0o600 {
		t.Fatalf("unexpected store mode: %o", details.Mode().Perm())
	}

	reopened, err := NewStore(dataDir, keyPath)
	if err != nil {
		t.Fatal(err)
	}
	material, err := reopened.Get("apiyi/integration")
	if err != nil {
		t.Fatal(err)
	}
	if material.Value != secret {
		t.Fatal("reopened secret does not match")
	}
	rotated, err := reopened.Rotate("apiyi/integration", "sk-rotated-secret-value", 1)
	if err != nil {
		t.Fatal(err)
	}
	if rotated.Version != 2 || rotated.Fingerprint == created.Fingerprint {
		t.Fatal("rotation metadata did not change")
	}
	if _, err := reopened.Rotate("apiyi/integration", "stale", 1); !errors.Is(err, ErrVersionConflict) {
		t.Fatalf("expected version conflict, got %v", err)
	}
	if err := reopened.Delete("apiyi/integration", 2); err != nil {
		t.Fatal(err)
	}
	if _, err := reopened.Get("apiyi/integration"); !errors.Is(err, ErrSecretNotFound) {
		t.Fatalf("expected missing secret, got %v", err)
	}
}

func TestMasterKeyAndReferenceValidation(t *testing.T) {
	directory := t.TempDir()
	keyPath := filepath.Join(directory, "master")
	if err := os.WriteFile(keyPath, []byte("short"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := NewStore(filepath.Join(directory, "data"), keyPath); err == nil {
		t.Fatal("short master key was accepted")
	}
	if err := os.WriteFile(keyPath, []byte("0123456789abcdef0123456789abcdef"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(keyPath, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := NewStore(filepath.Join(directory, "data2"), keyPath); err == nil {
		t.Fatal("insecure master key mode was accepted")
	}
}
