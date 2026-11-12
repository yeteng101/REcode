FROM cbug/libuv-build:bookworm
WORKDIR /opt/recode
COPY clang_pipeline /opt/recode/clang_pipeline
ENV PYTHONPATH=/opt/recode PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
USER 65534:65534
