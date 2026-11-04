# Supply a digest-pinned base from your approved registry at build time.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends python3 clang libclang-dev llvm binutils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/recode
# Deliberately exclude validation/Gold, .git, credentials, demos and artifacts.
COPY clang_pipeline /opt/recode/clang_pipeline
ENV PYTHONPATH=/opt/recode PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
USER 65534:65534
