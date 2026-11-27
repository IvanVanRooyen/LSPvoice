# LSPcare voice agent: one image for the voice agent and the dashboard (different commands).
#
#   GIT_COMMIT=$(git rev-parse --short HEAD) docker compose build
#
# Secrets, call records and test results are never in the image: they are mounted at /data
# (see compose.yaml and docs/home-server.md).
FROM python:3.14-slim

# tzdata: transcripts and tickets use local time (TZ is set in compose.yaml).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

# Runs as an unprivileged user. The mounted private folder must be owned by this uid
# (10001) with mode 700, and its files 600: the agent refuses credentials others can read.
RUN groupadd --gid 10001 lspvoice \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin lspvoice

WORKDIR /app
COPY voice-agent/requirements.txt voice-agent/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r voice-agent/requirements.txt

# Reviewed knowledge (fingerprinted; .gitattributes keeps it LF), the two shared helpers from
# mock-backend, the documentation shown on the dashboard, and the agent itself.
COPY knowledge knowledge
COPY mock-backend/private_files.py mock-backend/static_inventory.py mock-backend/
COPY docs docs
COPY README.md README.md
COPY voice-agent voice-agent

ARG GIT_COMMIT=unknown
ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LSPCARE_VERSION=${GIT_COMMIT} \
    LSPCARE_PRIVATE_DIR=/data/private \
    LSPCARE_RUNS_DIR=/data/runs \
    TZ=Australia/Adelaide

WORKDIR /app/voice-agent
USER 10001:10001
EXPOSE 8101

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8101/health', timeout=4)"]

# One worker: call state (sessions, nonces, ticket status) is held in memory.
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8101", "--workers", "1", "--no-access-log"]
