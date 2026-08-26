FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/ark-unpacker
COPY vendor/Ark-Unpacker/pyproject.toml vendor/Ark-Unpacker/poetry.lock ./
RUN python -m pip install --no-cache-dir "poetry>=2.0,<3.0" \
    && poetry install --only main --no-root --no-ansi
COPY vendor/Ark-Unpacker/ ./

WORKDIR /app
COPY requirements-service.txt ./
RUN python -m pip install --no-cache-dir -r requirements-service.txt \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/* /root/.cache
COPY ark_resource_service/ ./ark_resource_service/
COPY vendor/Ark-Unpacker/LICENSE /licenses/Ark-Unpacker-LICENSE
RUN mkdir -p /state /output && chown -R 501:20 /state /output

EXPOSE 8080
CMD ["python", "-m", "ark_resource_service"]
