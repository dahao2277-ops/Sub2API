ARG BASE_IMAGE=ai99t/commercial-core@sha256:d4d77063ec69c7715f5378576b4d7d0d513ac46f3e51ab08732343a99885f05f
FROM ${BASE_IMAGE}

ARG CORE_COMMIT
ARG BRIDGE_COMMIT
ARG BASE_IMAGE_DIGEST
LABEL org.opencontainers.image.revision=${BRIDGE_COMMIT}
LABEL ai16t.runtime.role="commercial-core-blue"
LABEL ai16t.runtime.core-commit=${CORE_COMMIT}
LABEL ai16t.runtime.stable-base-digest=${BASE_IMAGE_DIGEST}
LABEL ai16t.runtime.financial-code="unmodified-stable-base"

COPY --chown=65532:65532 core_bridge.py /bridge/core_bridge.py
COPY --chown=65532:65532 apiyi_provider.py /bridge/apiyi_provider.py
COPY --chown=65532:65532 secret_provider_client.py /bridge/secret_provider_client.py
COPY --chown=65532:65532 core_blue_entrypoint.py /bridge/core_blue_entrypoint.py

USER 65532:65532
