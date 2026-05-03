# URL Shortener — FastAPI + Redis

A production-grade URL shortening service deployed as a containerized, two-component stack. Built for WCU CSCI 402 as a demonstration of cloud infrastructure principles: containerization, CI/CD automation, registry integration, and Defense-in-Depth security hardening.

---

## Table of Contents

1. [Vision](#1-vision)
2. [Proposal](#2-proposal)
3. [Build Process](#3-build-process)
4. [Networking](#4-networking)
5. [CI/CD Pipeline](#5-cicd-pipeline)
6. [Security — Defense in Depth](#6-security--defense-in-depth)
7. [How to Launch the Stack](#7-how-to-launch-the-stack)
8. [CloudLab Deployment](#8-cloudlab-deployment)
9. [API Reference](#9-api-reference)

---

## 1. Vision

The system is composed of exactly two containers that communicate over a private Docker bridge network:

```
  Client (browser / curl)
        │
        │  HTTP  :8000
        ▼
┌───────────────────┐        Redis wire protocol
│   api (FastAPI)   │ ──────────────────────────▶ ┌───────────────┐
│   Python 3.12     │        TCP :6379             │  redis        │
│   Uvicorn ASGI    │ ◀────────────────────────── │  redis:7-alpine│
└───────────────────┘                              └───────────────┘
        │
        │  Bridge network: app-network
        └──────────────────────────────────────────────────────────
```

**Component 1 — `api` (FastAPI/Python)**
Accepts `POST /shorten` with a long URL, generates a 7-character base-62 short code, stores the mapping in Redis with a configurable TTL, and returns the short URL. A `GET /{code}` request resolves the code and issues a `301 Moved Permanently` redirect.

**Component 2 — `redis` (Redis 7)**
Acts as the primary datastore. Every URL mapping is stored as a Redis `SETEX` key (`url:{code}`) with an expiry, making TTL-based link expiration automatic and O(1).

**Communication protocol:** Redis wire protocol over TCP port 6379, addressed by container name (`redis`) via Docker's embedded DNS resolver.

---

## 2. Proposal

| Component | Base Image | Justification |
|-----------|-----------|---------------|
| `api` | `python:3.12-slim` | Official Python image on Debian slim — small footprint (~50 MB), no unnecessary packages, long-term support track. Alpine was considered but discarded because Alpine's musl libc causes subtle incompatibilities with some Python C-extension wheels. |
| `redis` | `redis:7-alpine` | The official Redis image on Alpine Linux. Redis has no Python dependencies so Alpine's musl libc limitations do not apply here. Alpine produces an image of ~10 MB, significantly smaller than the Debian variant. |

---

## 3. Build Process

The `api` service uses a **multi-stage Dockerfile** (`app/Dockerfile`). Each instruction is explained below.

```dockerfile
# --- Stage 1: Builder ---
FROM python:3.12-slim AS builder
```
We start from the official `python:3.12-slim` image. Using a named stage (`AS builder`) means we can later copy only the installed packages without bringing in pip, build tools, or intermediate layer artifacts. This keeps the final image lean and reduces the attack surface.

```dockerfile
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt
```
Dependencies are installed into `/install` instead of the system site-packages. `--no-cache-dir` avoids writing pip's download cache to the image layer, saving space. Separating the dependency install from the code copy means Docker can reuse this layer from cache on every rebuild unless `requirements.txt` changes — a significant speed improvement in CI.

```dockerfile
# --- Stage 2: Runtime ---
FROM python:3.12-slim
```
We start the final image from the same slim base. This stage never sees pip or the build layer — only the compiled packages are copied in.

```dockerfile
RUN groupadd --gid 1001 appgroup \
 && useradd  --uid 1001 --gid appgroup --no-create-home --shell /sbin/nologin appuser
```
A dedicated non-root user and group are created with a fixed UID/GID. This UID matches the `user:` directive in `docker-compose.yml`, ensuring the process has no elevated privileges even if it escapes the container.

```dockerfile
WORKDIR /app
COPY --from=builder /install /usr/local
COPY main.py .
RUN chown -R appuser:appgroup /app
USER appuser
```
Packages are copied from the builder stage into `/usr/local` (on Python's default path), application code is copied in, and ownership is locked to the non-root user before switching to it.

```dockerfile
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "..."
```
`EXPOSE` is metadata that documents the port to other developers and tooling. The `HEALTHCHECK` instruction lets Docker and Compose track container health without an external script — it polls the `/health` endpoint, which in turn pings Redis. `start_period=15s` gives the FastAPI app time to connect to Redis before the health check is evaluated.

```dockerfile
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```
`CMD` is expressed in *exec* form (a JSON array) rather than shell form. Exec form runs the process directly — not wrapped in `/bin/sh -c` — so Docker's `SIGTERM` signal reaches the Uvicorn process immediately, enabling graceful shutdown.

---

## 4. Networking

**Bridge network (`app-network`)**

Both containers are attached to a user-defined bridge network called `app-network`. User-defined bridges are preferred over the default bridge for two reasons:

1. **DNS resolution by container name:** Docker's embedded DNS resolver automatically maps the service name `redis` to the Redis container's IP address. The FastAPI app therefore connects with `REDIS_HOST=redis` — no hardcoded IPs that would break on container restart.
2. **Network isolation:** Only containers explicitly attached to `app-network` can reach each other. The Redis port (6379) is never published to the host, so it is inaccessible from outside the Docker network. The only externally reachable port is `8000` on the `api` service.

**Request flow:**

```
curl POST http://<host>:8000/shorten
    → Uvicorn (api container, port 8000)
    → generate_short_code()
    → redis_client.setex("url:abc1234", ttl, original_url)
    → Redis container (hostname: redis, port: 6379, bridge network)
    ← OK
    ← {"short_url": "http://<host>:8000/abc1234", ...}
```

---

## 5. CI/CD Pipeline

The pipeline is defined in `.github/workflows/deploy.yml` and consists of two sequential jobs.

**Job 1 — `build-and-push` (GitHub-hosted runner)**

Triggered on every push to `main`. The job:

1. Checks out the repository.
2. Authenticates to Docker Hub using repository secrets (`DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`).
3. Uses `docker/metadata-action` to generate two tags: a SHA-prefixed tag for traceability (e.g. `sha-a1b2c3d`) and `latest` for the HEAD of main.
4. Builds the image from `./app/Dockerfile` using `docker/build-push-action` with GitHub Actions cache (`type=gha`) for fast incremental builds.
5. Pushes both tags to Docker Hub.

**Job 2 — `deploy` (self-hosted CloudLab runner)**

Runs only after `build-and-push` succeeds. The job:

1. Pulls the new `latest` image from Docker Hub directly on the CloudLab node.
2. Sets `DOCKER_IMAGE` to the registry tag so Compose uses the remote image rather than building locally.
3. Runs `docker compose up -d` — Docker starts a new container from the fresh image and stops the old one, achieving a near-zero-downtime rolling update.
4. Verifies deployment by curling `/health` and printing `docker compose ps`.

**Setting up the self-hosted runner on CloudLab:**

```bash
# On the CloudLab node, after provisioning
mkdir actions-runner && cd actions-runner
curl -o actions-runner-linux-x64.tar.gz -L \
  https://github.com/actions/runner/releases/download/v2.317.0/actions-runner-linux-x64-2.317.0.tar.gz
tar xzf ./actions-runner-linux-x64.tar.gz
./config.sh --url https://github.com/<YOUR_USERNAME>/<YOUR_REPO> --token <RUNNER_TOKEN>
./run.sh
```
Obtain `<RUNNER_TOKEN>` from **GitHub → Repository → Settings → Actions → Runners → New self-hosted runner**.

---

## 6. Security — Defense in Depth

This stack implements multiple independent security layers (Defense in Depth) as covered in Lecture 12. No single misconfiguration can fully compromise the system.

### 6.1 Non-root User

Both containers run as non-root UIDs (1001 for `api`, 999 for `redis`). If a vulnerability in the application code were exploited, the attacker would hold only the privileges of an unprivileged user — not root inside the container, and certainly not root on the host.

```yaml
# docker-compose.yml
user: "1001:1001"   # api service
user: "999:999"     # redis service
```

The `api` Dockerfile creates this user explicitly and switches to it before the `CMD` instruction, making the non-root runtime the default even if the `user:` directive is omitted from Compose.

### 6.2 Capability Dropping

Linux capabilities divide the privileges of the root account into distinct units. By default, Docker grants containers a set of ~14 capabilities. This stack drops all of them and adds none back:

```yaml
cap_drop:
  - ALL
```

The relevant capability that is explicitly *not* granted is `CAP_SYS_ADMIN`, which would allow mounting filesystems, modifying kernel parameters, and more. Neither the FastAPI app nor Redis requires any elevated capability to function, so dropping everything is safe and significantly reduces the blast radius of a container breakout.

### 6.3 Read-only Filesystem

```yaml
read_only: true
tmpfs:
  - /tmp:size=64m,mode=1777
```

The container's root filesystem is mounted read-only. An attacker who gains code execution cannot write a backdoor, modify the application binary, or install tools. The `/tmp` directory is provided as a tmpfs (in-memory) mount for legitimate temporary file needs.

### 6.4 No Privilege Escalation

```yaml
security_opt:
  - no-new-privileges:true
```

This sets the Linux `no_new_privs` bit on the container process. Even if a setuid binary exists on the image, it cannot be used to gain elevated privileges.

### 6.5 Resource Limits (cgroup v2)

```yaml
deploy:
  resources:
    limits:
      cpus: "0.50"
      memory: 256M
```

CPU and memory limits are enforced by the kernel's cgroup v2 subsystem. This serves two purposes: it prevents a misbehaving or malicious workload from starving other containers on the same host (denial-of-service containment), and it demonstrates awareness of multi-tenant resource isolation — a key concept for cloud deployments.

---

## 7. How to Launch the Stack

### Prerequisites

- Docker Desktop ≥ 24.0 (Mac/Windows) or Docker Engine + Compose plugin (Linux)
- Git

### Clone and run locally

```bash
git clone https://github.com/<YOUR_USERNAME>/<YOUR_REPO>.git
cd <YOUR_REPO>

# Build the custom image and start both containers
docker compose up --build -d

# Confirm both containers are healthy
docker compose ps
```

The API will be available at `http://localhost:8000`. Interactive API docs (Swagger UI) are at `http://localhost:8000/docs`.

### Test the service

```bash
# Shorten a URL
curl -X POST http://localhost:8000/shorten \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.example.com/some/very/long/path?q=1"}'

# Example response:
# {
#   "short_code": "aB3kX7z",
#   "short_url": "http://localhost:8000/aB3kX7z",
#   "original_url": "https://www.example.com/some/very/long/path?q=1",
#   "ttl_seconds": 2592000
# }

# Follow the redirect
curl -L http://localhost:8000/aB3kX7z

# Health check
curl http://localhost:8000/health
```

### Stop the stack

```bash
docker compose down           # stop and remove containers
docker compose down -v        # also remove the Redis data volume
```

---

## 8. CloudLab Deployment

### One-time setup on CloudLab

```bash
# 1. Install Docker Engine
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker $USER && newgrp docker

# 2. Clone the repository
git clone https://github.com/<YOUR_USERNAME>/<YOUR_REPO>.git
cd <YOUR_REPO>

# 3. Install and register the GitHub Actions self-hosted runner
#    (see Section 5 for the full commands)

# 4. Launch the stack using the pre-built registry image
DOCKER_IMAGE=<YOUR_DOCKERHUB_USERNAME>/url-shortener:latest \
  docker compose up -d
```

Subsequent deployments are fully automated — push to `main` and the GitHub Actions pipeline handles the rest.

---

## 9. API Reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Service info and available endpoints |
| `GET` | `/health` | Liveness probe (returns 200 if Redis is reachable) |
| `POST` | `/shorten` | Shorten a URL; body: `{"url": "...", "ttl_seconds": 2592000}` |
| `GET` | `/{code}` | Resolve a short code and redirect (301) |

Interactive documentation is auto-generated by FastAPI at `/docs` (Swagger UI) and `/redoc` (ReDoc).
