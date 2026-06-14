---
name: private-dashboard-domain-deploy
description: Deploy a private dashboard preview behind a custom domain with HTTPS and Authelia-protected one-page login, including read-only surface checks and quick domain verification.
version: 1.0.0
author: CGTech
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [dashboard, caddy, authelia, totp, domain, deployment]
    category: devops
    requires_toolsets: [terminal]
---

# Private Dashboard Domain Deploy

## 1. Purpose
Use this skill for private dashboard preview rollout on a personal domain with HTTPS and protected login flow. The flow preserves dashboard read-only behavior and focuses on validation of route, certificate, and auth entrypoint.

## 2. Preconditions
- Deployment stack is in `/home/agentuser/eStatements/deploy/dashboard-totp`.
- DNS control for dashboard domain and auth domain is available.
- Docker and `docker compose` are available.
- Access to `curl`, `dig`, and `sed`.

## 3. Core Flow
1. Keep DB write and dashboard container behavior unchanged.
2. Confirm `deploy/dashboard-totp/.env` is populated from `.env.example`.
3. Verify Authelia user config exists in `deploy/dashboard-totp/authelia/users_database.yml`.
4. Start/update stack:
   ```bash
   cd /home/agentuser/eStatements/deploy/dashboard-totp
   docker compose up -d
   ```
5. Verify service health:
   ```bash
   cd /home/agentuser/eStatements/deploy/dashboard-totp
   docker compose ps
   ```
6. Verify HTTPS endpoints:
   ```bash
   curl -I https://<dashboard-domain>/
   curl -I https://<auth-domain>/
   ```
   Expect dashboard domain to redirect to auth domain and auth domain to return `200`.

## 4. Caddy + Auth Entrypoint
- Typical live ingress path is: `dashboard.cgtec.uk` and `auth.dashboard.cgtec.uk`.
- Dashboard host should reverse proxy to `dashboard-totp-caddy-1:8080`.
- If shared gateway Caddy manages the public ingress, its site block should only route these two hostnames to the local auth-enabled caddy service.

## 5. TOTP Enrollment
- Open auth domain in browser, initiate login.
- If filesystem notifier is used, read the current code from:
  ```bash
  sudo sed -n '1,200p' /home/agentuser/eStatements/deploy/dashboard-totp/authelia/notification.txt
  ```
- Enter six-digit code in browser prompt.
- If the code expires, trigger resend and re-read notification output.

## 6. Optional Cloudflare Tunnel Variant
- Keep the local stack unchanged.
- Route public hostname(s) through tunnel ingress to `http://dashboard-totp-caddy-1:8080`.
- Validate with the same `curl` endpoint checks.
- Typical tunnel flow is suitable when you want no direct public port exposure on the host.

## 7. Read-only Contract
- Dashboard preview route should remain non-mutating for end users.
- Verify no public write routes are exposed from this ingress path.

## 8. Rollback
- Restore previous gateway Caddy config and restart gateway service.
- Or stop the stack entirely with:
  ```bash
  cd /home/agentuser/eStatements/deploy/dashboard-totp
  docker compose down
  ```
