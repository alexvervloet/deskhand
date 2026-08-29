# Multi-stage: build the SPA with node, then serve it and the API from one
# Python image. The worker runs from this same image with a different command,
# so the two process groups can never drift out of sync with each other.

# --- stage 1: build the frontend -----------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- stage 2: python runtime ---------------------------------------------
FROM python:3.13-slim AS app
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY deskhand ./deskhand
COPY evals ./evals
COPY migrations ./migrations
COPY check_setup.py entrypoint.sh ./
COPY --from=web /web/dist ./frontend/dist
RUN chmod +x entrypoint.sh

# Drop root. Nothing in here needs it: the app binds 8000, which is above the
# privileged range, and writes nothing to disk — every side effect it has is a
# row in Postgres. The files are left owned by root and readable, so the
# process cannot rewrite its own code or the migrations it is about to apply.
RUN useradd --create-home --uid 10001 deskhand
USER deskhand

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=10 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"

ENTRYPOINT ["./entrypoint.sh"]
CMD ["uvicorn", "deskhand.main:app", "--host", "0.0.0.0", "--port", "8000"]
