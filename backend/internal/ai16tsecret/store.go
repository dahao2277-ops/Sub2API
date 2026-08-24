package ai16tsecret

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"sync"
	"time"
)

const (
	storeFormatVersion = 1
	storeFileName      = "secrets.json"
	maximumSecretBytes = 16 * 1024
)

var (
	ErrInvalidReference = errors.New("invalid secret reference")
	ErrSecretNotFound   = errors.New("secret reference is unavailable")
	ErrVersionConflict  = errors.New("secret version conflict")
	validReference      = regexp.MustCompile(`^[a-z0-9][a-z0-9._/-]{1,127}$`)
)

type Metadata struct {
	Reference   string    `json:"secret_ref"`
	Version     int       `json:"version"`
	Fingerprint string    `json:"fingerprint"`
	Last4       string    `json:"last4"`
	CreatedAt   time.Time `json:"created_at"`
	RotatedAt   time.Time `json:"rotated_at"`
}

type Material struct {
	Metadata
	Value string `json:"value"`
}

type encryptedRecord struct {
	Version     int       `json:"version"`
	Nonce       string    `json:"nonce"`
	Ciphertext  string    `json:"ciphertext"`
	Fingerprint string    `json:"fingerprint"`
	Last4       string    `json:"last4"`
	CreatedAt   time.Time `json:"created_at"`
	RotatedAt   time.Time `json:"rotated_at"`
}

type diskState struct {
	Format  int                        `json:"format"`
	Records map[string]encryptedRecord `json:"records"`
}

type Store struct {
	mu      sync.RWMutex
	dir     string
	path    string
	key     []byte
	aead    cipher.AEAD
	records map[string]encryptedRecord
	now     func() time.Time
}

func NewStore(dataDir, masterKeyPath string) (*Store, error) {
	key, err := readMasterKey(masterKeyPath)
	if err != nil {
		return nil, err
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, fmt.Errorf("configure secret cipher: %w", err)
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil, fmt.Errorf("configure secret AEAD: %w", err)
	}
	if err := ensurePrivateDirectory(dataDir); err != nil {
		return nil, err
	}
	store := &Store{
		dir:     dataDir,
		path:    filepath.Join(dataDir, storeFileName),
		key:     key,
		aead:    aead,
		records: make(map[string]encryptedRecord),
		now:     func() time.Time { return time.Now().UTC() },
	}
	if err := store.load(); err != nil {
		return nil, err
	}
	return store, nil
}

func (s *Store) Put(reference, value string) (Metadata, error) {
	if err := validate(reference, value); err != nil {
		return Metadata{}, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, exists := s.records[reference]; exists {
		return Metadata{}, ErrVersionConflict
	}
	now := s.now()
	record, err := s.encrypt(reference, 1, value, now, now)
	if err != nil {
		return Metadata{}, err
	}
	s.records[reference] = record
	if err := s.saveLocked(); err != nil {
		delete(s.records, reference)
		return Metadata{}, err
	}
	return metadata(reference, record), nil
}

func (s *Store) Rotate(reference, value string, expectedVersion int) (Metadata, error) {
	if err := validate(reference, value); err != nil {
		return Metadata{}, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	current, exists := s.records[reference]
	if !exists || expectedVersion < 1 || current.Version != expectedVersion {
		return Metadata{}, ErrVersionConflict
	}
	record, err := s.encrypt(
		reference,
		expectedVersion+1,
		value,
		current.CreatedAt,
		s.now(),
	)
	if err != nil {
		return Metadata{}, err
	}
	s.records[reference] = record
	if err := s.saveLocked(); err != nil {
		s.records[reference] = current
		return Metadata{}, err
	}
	return metadata(reference, record), nil
}

func (s *Store) Get(reference string) (Material, error) {
	if !validReference.MatchString(reference) {
		return Material{}, ErrInvalidReference
	}
	s.mu.RLock()
	record, exists := s.records[reference]
	s.mu.RUnlock()
	if !exists {
		return Material{}, ErrSecretNotFound
	}
	value, err := s.decrypt(reference, record)
	if err != nil {
		return Material{}, err
	}
	return Material{Metadata: metadata(reference, record), Value: value}, nil
}

func (s *Store) GetMetadata(reference string) (Metadata, error) {
	if !validReference.MatchString(reference) {
		return Metadata{}, ErrInvalidReference
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	record, exists := s.records[reference]
	if !exists {
		return Metadata{}, ErrSecretNotFound
	}
	return metadata(reference, record), nil
}

func (s *Store) Delete(reference string, expectedVersion int) error {
	if !validReference.MatchString(reference) {
		return ErrInvalidReference
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	current, exists := s.records[reference]
	if !exists || expectedVersion < 1 || current.Version != expectedVersion {
		return ErrVersionConflict
	}
	delete(s.records, reference)
	if err := s.saveLocked(); err != nil {
		s.records[reference] = current
		return err
	}
	return nil
}

func (s *Store) encrypt(
	reference string,
	version int,
	value string,
	createdAt time.Time,
	rotatedAt time.Time,
) (encryptedRecord, error) {
	nonce := make([]byte, s.aead.NonceSize())
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		return encryptedRecord{}, fmt.Errorf("create secret nonce: %w", err)
	}
	ciphertext := s.aead.Seal(nil, nonce, []byte(value), associatedData(reference, version))
	return encryptedRecord{
		Version:     version,
		Nonce:       base64.RawStdEncoding.EncodeToString(nonce),
		Ciphertext:  base64.RawStdEncoding.EncodeToString(ciphertext),
		Fingerprint: s.fingerprint(value),
		Last4:       last4(value),
		CreatedAt:   createdAt,
		RotatedAt:   rotatedAt,
	}, nil
}

func (s *Store) decrypt(reference string, record encryptedRecord) (string, error) {
	nonce, err := base64.RawStdEncoding.DecodeString(record.Nonce)
	if err != nil {
		return "", errors.New("secret ciphertext is invalid")
	}
	ciphertext, err := base64.RawStdEncoding.DecodeString(record.Ciphertext)
	if err != nil {
		return "", errors.New("secret ciphertext is invalid")
	}
	plaintext, err := s.aead.Open(nil, nonce, ciphertext, associatedData(reference, record.Version))
	if err != nil {
		return "", errors.New("secret ciphertext authentication failed")
	}
	return string(plaintext), nil
}

func (s *Store) fingerprint(value string) string {
	mac := hmac.New(sha256.New, s.key)
	_, _ = mac.Write([]byte("ai16t-secret-fingerprint-v1\x00"))
	_, _ = mac.Write([]byte(value))
	return hex.EncodeToString(mac.Sum(nil))
}

func (s *Store) load() error {
	payload, err := os.ReadFile(s.path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("read encrypted secret store: %w", err)
	}
	details, err := os.Lstat(s.path)
	if err != nil || !details.Mode().IsRegular() || details.Mode().Perm() != 0o600 {
		return errors.New("encrypted secret store must be a regular mode 0600 file")
	}
	var state diskState
	if err := json.Unmarshal(payload, &state); err != nil {
		return errors.New("encrypted secret store is corrupted")
	}
	if state.Format != storeFormatVersion || state.Records == nil {
		return errors.New("encrypted secret store format is unsupported")
	}
	for reference, record := range state.Records {
		if !validReference.MatchString(reference) || record.Version < 1 {
			return errors.New("encrypted secret store contains invalid metadata")
		}
		if _, err := s.decrypt(reference, record); err != nil {
			return err
		}
	}
	s.records = state.Records
	return nil
}

func (s *Store) saveLocked() error {
	payload, err := json.Marshal(diskState{Format: storeFormatVersion, Records: s.records})
	if err != nil {
		return fmt.Errorf("encode encrypted secret store: %w", err)
	}
	temporary, err := os.OpenFile(
		filepath.Join(s.dir, ".secrets.tmp"),
		os.O_CREATE|os.O_TRUNC|os.O_WRONLY,
		0o600,
	)
	if err != nil {
		return fmt.Errorf("create encrypted secret store: %w", err)
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
		return fmt.Errorf("secure encrypted secret store: %w", err)
	}
	if _, err := temporary.Write(payload); err != nil {
		return fmt.Errorf("write encrypted secret store: %w", err)
	}
	if err := temporary.Sync(); err != nil {
		return fmt.Errorf("sync encrypted secret store: %w", err)
	}
	if err := temporary.Close(); err != nil {
		return fmt.Errorf("close encrypted secret store: %w", err)
	}
	if err := os.Rename(temporaryName, s.path); err != nil {
		return fmt.Errorf("commit encrypted secret store: %w", err)
	}
	directory, err := os.Open(s.dir)
	if err != nil {
		return fmt.Errorf("open secret store directory: %w", err)
	}
	defer directory.Close()
	if err := directory.Sync(); err != nil {
		return fmt.Errorf("sync secret store directory: %w", err)
	}
	committed = true
	return nil
}

func readMasterKey(path string) ([]byte, error) {
	if path == "" {
		return nil, errors.New("secret master key path is required")
	}
	details, err := os.Lstat(path)
	if err != nil || !details.Mode().IsRegular() || details.Mode().Perm() != 0o600 {
		return nil, errors.New("secret master key must be a regular mode 0600 file")
	}
	payload, err := os.ReadFile(path)
	if err != nil {
		return nil, errors.New("secret master key is unavailable")
	}
	if len(payload) == 32 {
		return append([]byte(nil), payload...), nil
	}
	decoded, err := base64.RawStdEncoding.DecodeString(string(payload))
	if err != nil || len(decoded) != 32 {
		return nil, errors.New("secret master key must contain exactly 32 bytes")
	}
	return decoded, nil
}

func ensurePrivateDirectory(path string) error {
	if path == "" {
		return errors.New("secret data directory is required")
	}
	if err := os.MkdirAll(path, 0o700); err != nil {
		return fmt.Errorf("create secret data directory: %w", err)
	}
	if err := os.Chmod(path, 0o700); err != nil {
		return fmt.Errorf("secure secret data directory: %w", err)
	}
	details, err := os.Lstat(path)
	if err != nil || !details.IsDir() || details.Mode().Perm() != 0o700 {
		return errors.New("secret data directory must have mode 0700")
	}
	return nil
}

func validate(reference, value string) error {
	if !validReference.MatchString(reference) {
		return ErrInvalidReference
	}
	if value == "" || len(value) > maximumSecretBytes {
		return errors.New("secret material is invalid")
	}
	return nil
}

func associatedData(reference string, version int) []byte {
	return []byte(reference + "\x00" + strconv.Itoa(version))
}

func metadata(reference string, record encryptedRecord) Metadata {
	return Metadata{
		Reference:   reference,
		Version:     record.Version,
		Fingerprint: record.Fingerprint,
		Last4:       record.Last4,
		CreatedAt:   record.CreatedAt,
		RotatedAt:   record.RotatedAt,
	}
}

func last4(value string) string {
	if len(value) <= 4 {
		return value
	}
	return value[len(value)-4:]
}
