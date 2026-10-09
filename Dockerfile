FROM python:3.12-slim-bookworm AS schema-builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends flatbuffers-compiler \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/refresh_flatbuffer_schemas.py /tmp/refresh_flatbuffer_schemas.py
COPY scripts/flatbuffer_schemas/ /tmp/flatbuffer_schemas/
RUN python /tmp/refresh_flatbuffer_schemas.py \
      --destination /generated \
      --source-dir /tmp/flatbuffer_schemas \
      --work-dir /tmp/ark-fbs

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ffmpeg flatbuffers-compiler \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/ark-unpacker
COPY vendor/Ark-Unpacker/pyproject.toml vendor/Ark-Unpacker/poetry.lock ./
RUN python -m pip install --no-cache-dir "poetry>=2.0,<3.0" \
    && poetry install --only main --no-root --no-ansi
COPY vendor/Ark-Unpacker/ ./
COPY --from=schema-builder /generated/ /opt/ark-unpacker/src/fbs/CN/

WORKDIR /app
COPY requirements-service.txt ./
RUN python -m pip install --no-cache-dir -r requirements-service.txt \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/* /root/.cache
COPY ark_resource_service/ ./ark_resource_service/
COPY scripts/backfill_levels.py scripts/backfill_resources.py scripts/publish_backfill.py ./scripts/
COPY vendor/Ark-Unpacker/LICENSE /licenses/Ark-Unpacker-LICENSE
RUN mkdir -p /state /output && chown -R 501:20 /state /output

EXPOSE 8080
CMD ["python", "-m", "ark_resource_service"]
