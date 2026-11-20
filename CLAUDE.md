# Hermes Agent Railway Template

## Architecture

Thin wrapper around the official `nousresearch/hermes-agent` Docker image. There is no custom server or UI — Hermes's own dashboard, gateway, and s6-overlay supervision do the work.

- `Dockerfile` — `FROM` the official image pinned by SemVer tag + digest, copies the entrypoint, sets `HERMES_*` env defaults (`HERMES_HOME=/data/.hermes`, dashboard on `0.0.0.0`, gateway bootstrapped to running)
- `docker-entrypoint.sh` — compatibility shim: maps legacy `ADMIN_USERNAME`/`ADMIN_PASSWORD` to `HERMES_DASHBOARD_BASIC_AUTH_*`, generates a password if none is set, derives a stable session secret from the password, maps `PORT` to `HERMES_DASHBOARD_PORT`, then execs upstream `/opt/hermes/docker/entrypoint-dispatch.sh`
- `railway.toml` — Dockerfile build, start command, `/api/status` health check
- `docker-compose.yml` — local run with `./data` mounted at `/data`

## Key patterns

- All Hermes state (config, providers, channels, sessions) lives under `/data/.hermes` on the Railway volume and is managed through the official dashboard
- Existing deployments rely on the legacy variable names; keep the entrypoint mapping backward compatible
- Never use `latest`/`stable`; pin a release tag with its multi-arch index digest

## Upgrading Hermes

1. Pick the stable release (`gh release list -R NousResearch/hermes-agent`) and get its digest (`docker buildx imagetools inspect nousresearch/hermes-agent:<tag>`)
2. Confirm `/opt/hermes/docker/entrypoint-dispatch.sh` still exists in the new image and the basic-auth env vars are unchanged
3. Update the `FROM` line and the pinned image in `README.md`
4. Smoke test: start with an existing `/data` from the previous version and with a fresh volume; check `/api/status` returns 200 and `POST /auth/password-login` accepts `ADMIN_PASSWORD`
