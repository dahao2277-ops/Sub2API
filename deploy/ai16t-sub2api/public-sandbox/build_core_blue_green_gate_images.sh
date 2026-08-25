#!/bin/sh
set -eu

repo_root=$(git rev-parse --show-toplevel)
stable_bridge_commit=${CORE_GATE_STABLE_BRIDGE_COMMIT:-769cb142c5ae3e52670cb5f3f6841e322a679f04}
stable_core_commit=${CORE_GATE_STABLE_CORE_COMMIT:-d20dcf11aa2f75fa2854e405f8caa5eb331e0ecd}
stable_base_digest=${CORE_GATE_STABLE_BASE_DIGEST:-sha256:d4d77063ec69c7715f5378576b4d7d0d513ac46f3e51ab08732343a99885f05f}
base_image="ai99t/commercial-core@${stable_base_digest}"
candidate_commit=$(git -C "$repo_root" rev-parse HEAD)
candidate_short=$(git -C "$repo_root" rev-parse --short=12 HEAD)
blue_image=${CORE_GATE_BLUE_TAG:-ai99t/core-bg-blue:${candidate_short}}
green_image=${CORE_GATE_GREEN_TAG:-ai99t/core-bg-green:${candidate_short}}
mock_image=${CORE_GATE_MOCK_TAG:-ai99t/core-bg-mock:${candidate_short}}

if [ -n "$(git -C "$repo_root" status --porcelain --untracked-files=normal)" ]; then
  echo "refusing to build immutable gate images from a dirty worktree" >&2
  exit 1
fi

blue_context=$(mktemp -d "${TMPDIR:-/tmp}/ai99t-core-blue-context.XXXXXX")
cleanup() {
  find "$blue_context" -type f -delete
  rmdir "$blue_context"
}
trap cleanup EXIT HUP INT TERM
chmod 700 "$blue_context"

git -C "$repo_root" show "${stable_bridge_commit}:deploy/ai16t-sub2api/core_bridge.py" > "$blue_context/core_bridge.py"
git -C "$repo_root" show "${stable_bridge_commit}:deploy/ai16t-sub2api/apiyi_provider.py" > "$blue_context/apiyi_provider.py"
git -C "$repo_root" show "${stable_bridge_commit}:deploy/ai16t-sub2api/secret_provider_client.py" > "$blue_context/secret_provider_client.py"
cp "$repo_root/deploy/ai16t-sub2api/core_blue_entrypoint.py" "$blue_context/core_blue_entrypoint.py"

docker build --platform linux/amd64 \
  --build-arg "BASE_IMAGE=${base_image}" \
  --build-arg "CORE_COMMIT=${stable_core_commit}" \
  --build-arg "BRIDGE_COMMIT=${stable_bridge_commit}" \
  --build-arg "BASE_IMAGE_DIGEST=${stable_base_digest}" \
  -f "$repo_root/deploy/ai16t-sub2api/core-runtime-blue.Dockerfile" \
  -t "$blue_image" "$blue_context"

docker build --platform linux/amd64 \
  --build-arg "BASE_IMAGE=${base_image}" \
  --build-arg "CORE_COMMIT=${stable_core_commit}" \
  --build-arg "BRIDGE_COMMIT=${candidate_commit}" \
  --build-arg "BASE_IMAGE_DIGEST=${stable_base_digest}" \
  -f "$repo_root/deploy/ai16t-sub2api/core-runtime.Dockerfile" \
  -t "$green_image" "$repo_root"

docker build --platform linux/amd64 \
  --build-arg "BASE_IMAGE=${base_image}" \
  --build-arg "BRIDGE_COMMIT=${candidate_commit}" \
  -f "$repo_root/deploy/ai16t-sub2api/core-bg-mock.Dockerfile" \
  -t "$mock_image" "$repo_root"

docker image inspect "$blue_image" "$green_image" "$mock_image" \
  --format '{{index .RepoTags 0}} {{.Id}} {{.Architecture}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
