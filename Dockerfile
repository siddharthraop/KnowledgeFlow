# KnowledgeFlow API: the graph build, BM25 search, composer and checks, with the corpus baked in.
FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/siddharthraop/KnowledgeFlow" \
      org.opencontainers.image.licenses="MIT"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 KF_CORPUS=/app/corpus
WORKDIR /app
COPY pyproject.toml README.md ./
COPY kf kf
COPY api api
RUN pip install --no-cache-dir '.[api]' && pip uninstall -y pip
COPY corpus/tpu corpus/tpu
COPY corpus/nat corpus/nat
ARG CORPUS_COMMIT=main
ENV KF_CORPUS_COMMIT=${CORPUS_COMMIT}
RUN useradd --uid 10001 kf
USER kf
EXPOSE 8080
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]
