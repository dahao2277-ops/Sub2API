ARG BASE_IMAGE=ai99t/commercial-core@sha256:d4d77063ec69c7715f5378576b4d7d0d513ac46f3e51ab08732343a99885f05f
FROM ${BASE_IMAGE}

ARG BRIDGE_COMMIT
LABEL org.opencontainers.image.revision=${BRIDGE_COMMIT}
LABEL ai16t.runtime.role="core-blue-green-mock-provider"

COPY --chown=65532:65532 deploy/ai16t-sub2api/mock_provider.py /mock/mock_provider.py

USER 65532:65532
