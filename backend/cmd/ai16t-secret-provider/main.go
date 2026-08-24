package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/ai16tsecret"
)

const maximumRequestBytes = 32 * 1024

type mutationRequest struct {
	Operation       string `json:"operation"`
	Value           string `json:"value,omitempty"`
	ExpectedVersion int    `json:"expected_version,omitempty"`
}

type server struct {
	store *ai16tsecret.Store
}

func main() {
	if len(os.Args) < 2 {
		fatal("operation is required")
	}
	switch os.Args[1] {
	case "serve":
		serve()
	case "verify-store":
		verifyStore()
	case "put", "rotate", "delete", "metadata", "health":
		client(os.Args[1], os.Args[2:])
	default:
		fatal("unsupported operation")
	}
}

func verifyStore() {
	if _, err := ai16tsecret.NewStore(
		requiredEnv("AI16T_SECRET_DATA_DIR"),
		requiredEnv("AI16T_SECRET_MASTER_KEY_FILE"),
	); err != nil {
		fatal("encrypted secret store verification failed")
	}
	fmt.Println("SECRET_STORE_VERIFY=PASS")
}

func serve() {
	dataDir := requiredEnv("AI16T_SECRET_DATA_DIR")
	masterKeyPath := requiredEnv("AI16T_SECRET_MASTER_KEY_FILE")
	socketPath := requiredEnv("AI16T_SECRET_SOCKET")
	store, err := ai16tsecret.NewStore(dataDir, masterKeyPath)
	if err != nil {
		fatal(err.Error())
	}
	if err := prepareSocket(socketPath); err != nil {
		fatal(err.Error())
	}
	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		fatal("secret provider socket is unavailable")
	}
	defer listener.Close()
	if err := os.Chmod(socketPath, 0o600); err != nil {
		fatal("secret provider socket cannot be secured")
	}
	handler := &server{store: store}
	httpServer := &http.Server{
		Handler:           handler,
		ReadHeaderTimeout: 3 * time.Second,
		ReadTimeout:       5 * time.Second,
		WriteTimeout:      5 * time.Second,
		IdleTimeout:       15 * time.Second,
	}
	if err := httpServer.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
		fatal("secret provider stopped unexpectedly")
	}
}

func (s *server) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	writer.Header().Set("Content-Type", "application/json")
	writer.Header().Set("Cache-Control", "no-store")
	if request.URL.Path == "/health" && request.Method == http.MethodGet {
		writeJSON(writer, http.StatusOK, map[string]string{"status": "ok"})
		return
	}
	const prefix = "/v1/secrets/"
	if !strings.HasPrefix(request.URL.Path, prefix) {
		writeError(writer, http.StatusNotFound)
		return
	}
	reference, err := url.PathUnescape(strings.TrimPrefix(request.URL.EscapedPath(), prefix))
	if err != nil || reference == "" {
		writeError(writer, http.StatusBadRequest)
		return
	}
	switch request.Method {
	case http.MethodGet:
		if request.URL.Query().Get("metadata") == "1" {
			metadata, err := s.store.GetMetadata(reference)
			if err != nil {
				writeStoreError(writer, err)
				return
			}
			writeJSON(writer, http.StatusOK, metadata)
			return
		}
		material, err := s.store.Get(reference)
		if err != nil {
			writeStoreError(writer, err)
			return
		}
		writeJSON(writer, http.StatusOK, material)
	case http.MethodPost:
		var mutation mutationRequest
		decoder := json.NewDecoder(io.LimitReader(request.Body, maximumRequestBytes+1))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&mutation); err != nil {
			writeError(writer, http.StatusBadRequest)
			return
		}
		var metadata ai16tsecret.Metadata
		var err error
		switch mutation.Operation {
		case "put":
			metadata, err = s.store.Put(reference, mutation.Value)
		case "rotate":
			metadata, err = s.store.Rotate(reference, mutation.Value, mutation.ExpectedVersion)
		default:
			writeError(writer, http.StatusBadRequest)
			return
		}
		if err != nil {
			writeStoreError(writer, err)
			return
		}
		writeJSON(writer, http.StatusOK, metadata)
	case http.MethodDelete:
		expected, err := strconv.Atoi(request.URL.Query().Get("expected_version"))
		if err != nil || expected < 1 {
			writeError(writer, http.StatusBadRequest)
			return
		}
		if err := s.store.Delete(reference, expected); err != nil {
			writeStoreError(writer, err)
			return
		}
		writeJSON(writer, http.StatusOK, map[string]string{"status": "deleted"})
	default:
		writeError(writer, http.StatusMethodNotAllowed)
	}
}

func client(operation string, arguments []string) {
	socketPath := requiredEnv("AI16T_SECRET_SOCKET")
	httpClient := unixClient(socketPath)
	if operation == "health" {
		response := perform(httpClient, http.MethodGet, "http://unix/health", nil)
		defer response.Body.Close()
		if response.StatusCode != http.StatusOK {
			fatal("secret provider is not healthy")
		}
		fmt.Println("SECRET_PROVIDER_HEALTH=PASS")
		return
	}
	reference, expectedVersion := parseArguments(arguments)
	endpoint := "http://unix/v1/secrets/" + url.PathEscape(reference)
	var response *http.Response
	switch operation {
	case "put", "rotate":
		value, err := io.ReadAll(io.LimitReader(os.Stdin, maximumRequestBytes+1))
		if err != nil || len(value) == 0 || len(value) > maximumRequestBytes {
			fatal("secret input is invalid")
		}
		value = bytes.TrimRight(value, "\r\n")
		payload, err := json.Marshal(mutationRequest{
			Operation:       operation,
			Value:           string(value),
			ExpectedVersion: expectedVersion,
		})
		zero(value)
		if err != nil {
			fatal("secret request cannot be encoded")
		}
		response = perform(httpClient, http.MethodPost, endpoint, bytes.NewReader(payload))
		zero(payload)
	case "delete":
		endpoint += "?expected_version=" + strconv.Itoa(expectedVersion)
		response = perform(httpClient, http.MethodDelete, endpoint, nil)
	case "metadata":
		response = perform(httpClient, http.MethodGet, endpoint+"?metadata=1", nil)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, maximumRequestBytes))
	if err != nil || response.StatusCode != http.StatusOK {
		fatal("secret lifecycle operation failed")
	}
	if operation == "delete" {
		fmt.Println("SECRET_DELETED=PASS")
		return
	}
	var metadata ai16tsecret.Metadata
	if err := json.Unmarshal(body, &metadata); err != nil {
		fatal("secret metadata is invalid")
	}
	encoded, err := json.Marshal(metadata)
	if err != nil {
		fatal("secret metadata cannot be encoded")
	}
	fmt.Println(string(encoded))
}

func unixClient(socketPath string) *http.Client {
	transport := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{Timeout: 2 * time.Second}).DialContext(ctx, "unix", socketPath)
		},
		DisableCompression: true,
	}
	return &http.Client{Transport: transport, Timeout: 5 * time.Second}
}

func perform(client *http.Client, method, endpoint string, body io.Reader) *http.Response {
	request, err := http.NewRequest(method, endpoint, body)
	if err != nil {
		fatal("secret request cannot be created")
	}
	request.Header.Set("Content-Type", "application/json")
	response, err := client.Do(request)
	if err != nil {
		fatal("secret provider is unavailable")
	}
	return response
}

func parseArguments(arguments []string) (string, int) {
	if len(arguments) < 2 || arguments[0] != "--ref" {
		fatal("--ref is required")
	}
	reference := strings.TrimSpace(arguments[1])
	expectedVersion := 0
	if len(arguments) > 2 {
		if len(arguments) != 4 || arguments[2] != "--expected-version" {
			fatal("--expected-version is invalid")
		}
		value, err := strconv.Atoi(arguments[3])
		if err != nil || value < 1 {
			fatal("--expected-version is invalid")
		}
		expectedVersion = value
	}
	return reference, expectedVersion
}

func prepareSocket(path string) error {
	if path == "" {
		return errors.New("secret provider socket path is required")
	}
	directory := filepath.Dir(path)
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return errors.New("secret provider socket directory is unavailable")
	}
	if err := os.Chmod(directory, 0o700); err != nil {
		return errors.New("secret provider socket directory cannot be secured")
	}
	if details, err := os.Lstat(path); err == nil {
		if details.Mode()&os.ModeSocket == 0 {
			return errors.New("secret provider socket path is occupied")
		}
		if err := os.Remove(path); err != nil {
			return errors.New("stale secret provider socket cannot be removed")
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return errors.New("secret provider socket path is unavailable")
	}
	return nil
}

func requiredEnv(name string) string {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		fatal(name + " is required")
	}
	return value
}

func writeStoreError(writer http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, ai16tsecret.ErrSecretNotFound):
		writeError(writer, http.StatusNotFound)
	case errors.Is(err, ai16tsecret.ErrVersionConflict):
		writeError(writer, http.StatusConflict)
	case errors.Is(err, ai16tsecret.ErrInvalidReference):
		writeError(writer, http.StatusBadRequest)
	default:
		writeError(writer, http.StatusInternalServerError)
	}
}

func writeError(writer http.ResponseWriter, status int) {
	writeJSON(writer, status, map[string]string{"error": http.StatusText(status)})
}

func writeJSON(writer http.ResponseWriter, status int, value any) {
	payload, err := json.Marshal(value)
	if err != nil {
		http.Error(writer, "internal error", http.StatusInternalServerError)
		return
	}
	writer.WriteHeader(status)
	_, _ = writer.Write(payload)
}

func zero(payload []byte) {
	for index := range payload {
		payload[index] = 0
	}
}

func fatal(message string) {
	fmt.Fprintln(os.Stderr, message)
	os.Exit(1)
}
