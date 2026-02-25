FROM python:3.11-slim

LABEL maintainer="netauto-lab"
LABEL description="Network AI Agents – Ops and Engineering"

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

# Determine which agent to start based on AGENT_TYPE env var
CMD ["sh", "-c", "python -m ${AGENT_TYPE}_agent.main"]
