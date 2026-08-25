ARG BASE_IMAGE=ai99t/commercial-core@sha256:d4d77063ec69c7715f5378576b4d7d0d513ac46f3e51ab08732343a99885f05f
FROM ${BASE_IMAGE}

ARG BRIDGE_COMMIT
ARG CORE_COMMIT
ARG BASE_IMAGE_DIGEST
LABEL org.opencontainers.image.revision=${BRIDGE_COMMIT}
LABEL ai16t.runtime.role="commercial-core-green"
LABEL ai16t.runtime.core-commit=${CORE_COMMIT}
LABEL ai16t.runtime.stable-base-digest=${BASE_IMAGE_DIGEST}
LABEL ai16t.runtime.financial-code="candidate-bridge-on-stable-base"

COPY --chown=65532:65532 deploy/ai16t-sub2api/core_bridge.py /bridge/core_bridge.py
COPY --chown=65532:65532 deploy/ai16t-sub2api/apiyi_provider.py /bridge/apiyi_provider.py
COPY --chown=65532:65532 deploy/ai16t-sub2api/apiyi_transport.py /bridge/apiyi_transport.py
COPY --chown=65532:65532 deploy/ai16t-sub2api/apiyi_auth_probe.py /bridge/apiyi_auth_probe.py
COPY --chown=65532:65532 deploy/ai16t-sub2api/secret_provider_client.py /bridge/secret_provider_client.py
COPY --chown=65532:65532 deploy/ai16t-sub2api/core_evidence.py /evidence/core_evidence.py
COPY --chown=65532:65532 deploy/ai16t-sub2api/staging/core_backup.py /ops/core_backup.py

USER 65532:65532
