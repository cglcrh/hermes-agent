---
name: private-dashboard-domain-deploy
description: "Use when deploying or redeploying the private dashboard preview behind a personal domain with HTTPS + Caddy + Authelia TOTP, including cgtec.uk style rollout and first-run verification."
---

# Private Dashboard Domain Deploy

## Purpose
Use this skill when you need to publish the project dashboard behind a private domain with password+TOTP protection, public HTTPS, and a fast rollback-friendly, repeatable flow.

## Prerequisites
- Access to the deployment workspace:
  - `/home/agentuser/eStatements`
- DNS control for:
  - `<dashboard-domain>` (e.g. `dashboard.cgtec.uk`)
  - `<auth-domain>` (e.g. `auth.dashboard.cgtec.uk`)
- Docker and `docker compose` available
- `curl`, `dig`, and `sed` available

## Core Deployment Flow
1. Confirm source of truth
   - Keep compose + Caddy/Authelia runtime in `deploy/dashboard-totp/`.
   - Runtime files should stay local and ignored by git (`.env`, `authelia/users_database.yml`, sqlite db, notification file).
2. Configure environment
   - Copy `deploy/dashboard-totp/.env.example` to `deploy/dashboard-totp/.env`.
   - Fill values for dashboard container paths, domain names, and TOTP bootstrap password.
3. Build adapter-facing dashboard auth context
   - Ensure `deploy/dashboard-totp/authelia/users_database.yml` exists (from example if not already generated).
   - Seed `dashboard_viewer` entry if needed and keep password hashing format compatible with Authelia version.
4. Start/refresh stack
   - `cd /home/agentuser/eStatements/deploy/dashboard-totp`
   - `docker compose up -d`
5. Verify service health and certificate
   - `docker compose ps`
   - check both `dashboard-totp-caddy-1` and `dashboard-totp-authelia-1` are up
   - `curl -I https://<dashboard-domain>/`
   - `curl -I https://<auth-domain>/`
   - expected: dashboard domain returns redirect (`302`) to auth domain; auth domain returns `200`.
6. Connect front proxy if needed
   - If you need domain served through a shared gateway Caddy, add/replace the site block for both hostnames to reverse proxy `dashboard-totp-caddy-1:8080`.
   - Reload or restart that gateway after editing.

## TOTP Enrollment Verification
- In first login flow, copy the code from notifier output when no SMTP is configured:
  - `sudo sed -n '1,200p' /home/agentuser/eStatements/deploy/dashboard-totp/authelia/notification.txt`
- Fill the code in browser prompt and complete authenticator setup.
- If the code expires, request a resend and repeat from file.

## One-Command Smoke Check
Run this when DNS and stack are expected to be ready:
```
for h in <dashboard-domain> <auth-domain>; do
  echo "## $h"
  dig +short "$h" || true
  curl -I "https://$h/"
done
```

## Rollback
- Keep old `Caddyfile`/stack versions committed if the deployment is through another host Caddy.
- If rollback is needed, stop the private stack and restore prior proxy config, then restart affected gateway containers.

## Operational Notes
- Keep dashboard behavior read-only by design in this flow; do not add write routes for the public preview endpoint.
- `notification.txt` is filesystem notifier for local validation only, not a production outbound email path.
- This workflow is compatible with Azure DNS or Cloudflare DNS as long as A/AAAA + CNAME targets are correct and reachable.

## Cloudflare Tunnel Variant (Optional)
- Use this variant when dashboard traffic must be exposed only through Cloudflare Tunnel and not directly through public ingress on 80/443.
- Keep service stack local (`deploy/dashboard-totp`) unchanged; only switch the external ingress path.
- Required artifacts under this variant:
  - Cloudflare Tunnel token or cert auth file accessible by the runtime stack.
  - `cloudflared` credentials on host.
- Minimal flow:
  1. Keep `dashboard-totp-caddy-1` serving on `http://localhost:8080` internally.
  2. Start/verify tunnel connector on host:
     - `cloudflared tunnel run <tunnel-name>` (or `docker compose` tunnel service).
  3. Configure tunnel ingress:
     - `dashboard.cgtec.uk` -> `http://dashboard-totp-caddy-1:8080`
     - `auth.dashboard.cgtec.uk` -> `http://dashboard-totp-caddy-1:8080`
     - add `originRequest` `http_host_header` if proxying through another host.
  4. Ensure Caddy/Authelia still terminate auth internally as in base flow.
  5. Validate both hostnames:
     - `curl -I https://dashboard.cgtec.uk/`
     - `curl -I https://auth.dashboard.cgtec.uk/`
  6. Keep TLS at the edge (Cloudflare or origin cert) according to your tunnel policy.
- If you previously used a working tunnel route for another service, you can reuse the same operator pattern:
  - only replace the public hostname mapping target and restart the tunnel service after edit.
- Keep the same TOTP enrollment checks:
  - read code from `notification.txt` unless SMTP notifier is enabled.
