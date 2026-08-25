ARG BASE_IMAGE=ai99t/commercial-core:d20dcf11
FROM ${BASE_IMAGE}

ARG BRIDGE_COMMIT
LABEL org.opencontainers.image.revision=${BRIDGE_COMMIT}
LABEL ai16t.runtime.role="commercial-core-green"

COPY --chown=65532:65532 deploy/ai16t-sub2api/core_bridge.py /bridge/core_bridge.py
COPY --chown=65532:65532 deploy/ai16t-sub2api/apiyi_provider.py /bridge/apiyi_provider.py
COPY --chown=65532:65532 deploy/ai16t-sub2api/apiyi_transport.py /bridge/apiyi_transport.py
COPY --chown=65532:65532 deploy/ai16t-sub2api/apiyi_auth_probe.py /bridge/apiyi_auth_probe.py
COPY --chown=65532:65532 deploy/ai16t-sub2api/secret_provider_client.py /bridge/secret_provider_client.py
COPY --chown=65532:65532 deploy/ai16t-sub2api/core_evidence.py /evidence/core_evidence.py
COPY --chown=65532:65532 deploy/ai16t-sub2api/staging/core_backup.py /ops/core_backup.py

USER 65532:65532
