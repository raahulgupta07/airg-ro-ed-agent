# RO-ED AI Agent — Master + Column Agent Architecture
# Python 3.12 + FastAPI + Pure Vision AI (NO Tesseract)
# Build context: project root (RO-ED-Lang/)

# ---- Stage 1: Build frontend ----
FROM node:20-slim AS frontend-build

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: Backend + serve ----
FROM python:3.12-slim

WORKDIR /app

# System deps (mupdf for PDF rendering only — NO OCR)
RUN apt-get update && apt-get install -y \
    gcc g++ libmupdf-dev mupdf-tools curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps (cached layer)
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --timeout 300 -r requirements.txt

# Backend code — root files
COPY backend/*.py /app/

# Alembic config + migrations + one-shot SQLite migration script
COPY backend/alembic.ini /app/alembic.ini
COPY backend/alembic/ /app/alembic/
COPY backend/scripts/ /app/scripts/

# Backend code — pipeline
COPY backend/pipeline/ /app/pipeline/

# Backend code — platform features
COPY backend/agents/ /app/agents/

# Backend code — routes
COPY backend/routes/ /app/routes/

# Backend code — V10 PRO (shape validator + memory + 800 DPI digit-list + box detect)
COPY backend/v10_pro/ /app/v10_pro/
# Backend code — V11 (Master Router: per-page classify → V7 + V10 parallel → merge)
COPY backend/v11/ /app/v11/

# Backend code — storage abstraction (local + S3)
COPY backend/storage/ /app/storage/

# Backend code — RQ workers (queue + tasks)
COPY backend/jobs/ /app/jobs/

# Frontend from stage 1
COPY --from=frontend-build /frontend/build/ /app/frontend-build/

# Ensure all files are readable
RUN chmod -R a+r /app/

# Non-root user
RUN useradd -m -u 1000 appuser

# Data directories
RUN mkdir -p /app/data/results /app/data/uploads /app/data/jobs \
    && chown -R appuser:appuser /app

ENV PYTHONUNBUFFERED=1
EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:9000/api/health || exit 1

USER appuser

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000", "--workers", "2", "--limit-concurrency", "50", "--timeout-keep-alive", "300"]
