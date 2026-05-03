# =============================================================================
# Stage 1 — Builder
# Install Python dependencies into an isolated prefix so we can copy only the
# compiled packages into the final image (no pip, no build tools, smaller image).
# =============================================================================
FROM python:3.12-slim AS builder

WORKDIR /build

# Install dependencies into /install so we can copy them cleanly
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# =============================================================================
# Stage 2 — Runtime
# Lean production image: no pip, no build tools, runs as a non-root user.
# =============================================================================
FROM python:3.12-slim

# --- Security: create a dedicated non-root user and group ---
# UID/GID 1001 avoids collisions with common system accounts.
RUN groupadd --gid 1001 appgroup \
 && useradd  --uid 1001 --gid appgroup --no-create-home --shell /sbin/nologin appuser

WORKDIR /app

# Copy compiled packages from the builder stage
COPY --from=builder /install /usr/local

# Copy application source — only what the runtime needs
COPY main.py .

# Lock down ownership before switching user
RUN chown -R appuser:appgroup /app

# Switch to non-root user for all subsequent commands and the final process
USER appuser

EXPOSE 8000

# Docker-native health check — used by Compose depends_on and by orchestrators
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c \
        "import urllib.request, sys; \
         r = urllib.request.urlopen('http://localhost:8000/health'); \
         sys.exit(0 if r.status == 200 else 1)"

# Run uvicorn directly (no shell wrapper) so signals (SIGTERM) reach the process
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
