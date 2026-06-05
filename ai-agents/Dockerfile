FROM python:3.11-slim

LABEL maintainer="netauto-lab"
LABEL description="Network AI Agent – unified operations, engineering, and validation"

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Unified agent — single FastAPI app on port 8000
# UVICORN_WORKERS can be overridden via environment (e.g. in docker-compose.prod.yml)
CMD ["sh", "-c", "python -m uvicorn main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-1}"]
