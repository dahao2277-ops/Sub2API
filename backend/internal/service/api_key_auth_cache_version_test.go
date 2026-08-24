package service

import (
	"encoding/json"
	"testing"
	"time"
)

func TestAPIKeyService_RejectsV10AuthSnapshotWithoutModelsListConfig(t *testing.T) {
	groupID := int64(9)
	svc := &APIKeyService{}

	apiKey, ok, err := svc.applyAuthCacheEntry("k-legacy-models-list", &APIKeyAuthCacheEntry{
		Snapshot: &APIKeyAuthSnapshot{
			Version:  10,
			APIKeyID: 1,
			UserID:   2,
			GroupID:  &groupID,
			Status:   StatusActive,
			User: APIKeyAuthUserSnapshot{
				ID:          2,
				Status:      StatusActive,
				Role:        RoleUser,
				Balance:     10,
				Concurrency: 3,
			},
			Group: &APIKeyAuthGroupSnapshot{
				ID:               groupID,
				Name:             "openai",
				Platform:         PlatformOpenAI,
				Status:           StatusActive,
				SubscriptionType: SubscriptionTypeStandard,
				RateMultiplier:   1,
			},
		},
	})

	if err != nil {
		t.Fatalf("expected stale snapshot to be ignored without error, got %v", err)
	}
	if ok {
		t.Fatalf("expected v10 auth snapshot to be rejected after models_list_config was added")
	}
	if apiKey != nil {
		t.Fatalf("expected no API key from stale snapshot, got %#v", apiKey)
	}
}

func TestAPIKeyService_RejectsV15AuthSnapshotWithoutReasoningEffortPolicy(t *testing.T) {
	svc := &APIKeyService{}

	apiKey, ok, err := svc.applyAuthCacheEntry("k-legacy-reasoning-mappings", &APIKeyAuthCacheEntry{
		Snapshot: &APIKeyAuthSnapshot{Version: 15},
	})

	if err != nil {
		t.Fatalf("expected stale snapshot to be ignored without error, got %v", err)
	}
	if ok {
		t.Fatal("expected v15 auth snapshot to be rejected after reasoning effort policy was added")
	}
	if apiKey != nil {
		t.Fatalf("expected no API key from stale snapshot, got %#v", apiKey)
	}
}

func TestAPIKeyService_RejectsV20AuthSnapshotWithoutCanaryAdmissionFields(t *testing.T) {
	svc := &APIKeyService{}
	apiKey, ok, err := svc.applyAuthCacheEntry("k-v20", &APIKeyAuthCacheEntry{
		Snapshot: &APIKeyAuthSnapshot{Version: 20},
	})
	if err != nil || ok || apiKey != nil {
		t.Fatalf("expected v20 snapshot to fail closed, key=%#v ok=%v err=%v", apiKey, ok, err)
	}
}

func TestAPIKeyService_V21CanaryAdmissionFieldsSurviveJSONRoundTrip(t *testing.T) {
	created := time.Date(2026, time.August, 24, 12, 0, 0, 0, time.UTC)
	expires := created.Add(7 * 24 * time.Hour)
	snapshot := &APIKeyAuthSnapshot{
		Version:   apiKeyAuthSnapshotVersion,
		APIKeyID:  1,
		UserID:    2,
		Status:    StatusActive,
		CreatedAt: created,
		ExpiresAt: &expires,
		User: APIKeyAuthUserSnapshot{
			ID:                  2,
			Status:              StatusActive,
			APIKeyCount:         1,
			APIKeyCountResolved: true,
		},
		Group: &APIKeyAuthGroupSnapshot{
			ID:                  3,
			Name:                "CANARY-CUSTOMER-01",
			Status:              StatusActive,
			DefaultValidityDays: 7,
		},
	}
	payload, err := json.Marshal(&APIKeyAuthCacheEntry{Snapshot: snapshot})
	if err != nil {
		t.Fatal(err)
	}
	var restored APIKeyAuthCacheEntry
	if err := json.Unmarshal(payload, &restored); err != nil {
		t.Fatal(err)
	}
	materialized, ok, err := (&APIKeyService{}).applyAuthCacheEntry("k-v21", &restored)
	if err != nil || !ok || materialized == nil {
		t.Fatalf("expected v21 snapshot to restore, key=%#v ok=%v err=%v", materialized, ok, err)
	}
	if !materialized.CreatedAt.Equal(created) || materialized.ExpiresAt == nil ||
		!materialized.ExpiresAt.Equal(expires) || materialized.Group.DefaultValidityDays != 7 ||
		!materialized.User.APIKeyCountResolved || materialized.User.APIKeyCount != 1 {
		t.Fatalf("Canary admission fields did not survive: %#v", materialized)
	}
}
