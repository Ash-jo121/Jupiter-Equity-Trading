# Backend image for Railway. The dashboard deploys separately (see DEPLOY.md).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install the package first (its own layer) so code edits don't reinstall deps.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

# SQLite lives on a mounted volume so runs and reports survive redeploys.
# Set PAPER_DB_PATH=/data/paper_trading.db and attach a Railway volume at /data.
ENV PAPER_DB_PATH=/data/paper_trading.db
RUN mkdir -p /data

# Railway injects $PORT; run() binds 0.0.0.0:$PORT when PORT is present.
CMD ["python", "-c", "from jupiter_trading.api import run; run()"]
