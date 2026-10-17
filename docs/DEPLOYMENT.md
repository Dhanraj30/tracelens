# Deployment proposal (nothing deployed yet — needs your approval)

Verified against the providers' own pages on **17 Oct 2026**. Prices and limits change; re-check the linked pages before clicking.

## What has to run

| Piece | Image | Needs |
|---|---|---|
| TraceLens app (API + built dashboard) | `tracelens-app` (multi-stage Dockerfile, 207 MB) | ~256 MB RAM, port 8000, env `FALKORDB_HOST/PORT`, `AUTO_SEED=true`, optional `LLM_*` |
| FalkorDB | `falkordb/falkordb` (pinned digest in docker-compose.yml) | ~100 MB RAM for the demo graph, port 6379, no disk needed (the app re-seeds an empty graph) |

The app already **auto-seeds an empty graph** and `/api/health` reports node count, so a FalkorDB that restarts or resets is survivable: the demo comes back, only agent memory written since the reset is lost.

## Options

### Option A (recommended): Oracle Cloud Always Free VM, both containers via docker compose

- **Verified:** Always Free includes Arm Ampere A1 up to **2 OCPUs + 12 GB RAM** total (or 2 × AMD micro at 1 GB), **200 GB** block/boot storage, "free for the life of the account". Source: [Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm).
- **Why it fits:** both containers side by side with `docker compose up -d`, no sleeping, no 15-minute spin-down, FalkorDB keeps memory between demos. Nothing to change in the repo.
- **Catches:** sign-up asks for identity verification with a card (not charged on Always Free; the public sign-up page returned 403 to my fetch, so confirm on the form). Arm capacity in some regions is "out of host capacity" for hours; retry or pick another availability domain. Idle instances (under 20 % CPU/network for 7 days) can be reclaimed, so keep the health ping below. Open port 8000 (or put Caddy on 80/443) in the VCN security list.
- **Your clicks:** create account → choose home region → Compute → Create instance (Ampere A1, 1 OCPU / 6 GB, Ubuntu) → add your SSH key → VCN security list: allow TCP 8000 → SSH in, install Docker, `git clone`, `docker compose up -d --build`.
- **Cost:** $0.

### Option B: Render, two free web services (app + FalkorDB), no card

- **Verified:** Free web services get **750 instance-hours per workspace per month**, **spin down after 15 minutes** without inbound traffic and take ~1 minute to wake, **no persistent disk**, bandwidth counted against the free allowance. Source: [Render free docs](https://render.com/docs/free).
- **Why it fits:** Docker web services are allowed on Free; FalkorDB can run as a second web service (it only needs a port; Render requires an HTTP port check, so expose FalkorDB's 3000 Browser port for the health check and talk to 6379 over the private hostname).
- **Catches:** two always-on services = 2 × 730 h > 750 h/month, so **one of them will be suspended before month end** unless both are allowed to sleep. With spin-down, the first judge request waits ~1 minute and FalkorDB comes back empty (then auto-seeds, memory gone). Acceptable for a demo if you warm it up right before judging; not great for "memory across sessions".
- **Your clicks:** sign up → New → Web Service → connect the GitHub repo → Docker → env vars (`FALKORDB_HOST=<falkordb service hostname>`, `FALKORDB_PORT=6379`, `AUTO_SEED=true`) → second Web Service from image `falkordb/falkordb` with port 3000 → deploy.
- **Cost:** $0, but see the hours cap.

### Option C: Railway Free plan ($1/month credit, 3 services) or 30-day Trial ($5 credit)

- **Verified:** Free plan is $0 with **$1 of monthly usage credit, 1 vCPU / 0.5 GB per service, 1 project, 3 services**; Trial gives a one-time **$5 credit for 30 days**, no card. Source: [Railway pricing](https://railway.com/pricing).
- **Why it fits:** compose-like multi-service project, private networking between app and FalkorDB, no sleep. Hobby is $5/month if the credit runs out.
- **Catches:** $1/month of usage will not keep two containers running all month (two small services are roughly $5–8/month at Railway's rates); the Trial credit would cover the hackathon week. Treat this as "free until 18 Oct, then a $5 Hobby plan".
- **Your clicks:** sign up (GitHub) → New Project → Deploy from GitHub repo (Dockerfile detected) → Add service → Docker image `falkordb/falkordb` → set env vars → generate domain.

### Not recommended

- **Fly.io:** "New organizations don't have a free tier"; trial is 2 hours of machine runtime or 7 days; card required after; a shared-cpu-1x 256 MB machine is $2.19/month. Source: [Fly pricing](https://docs.fly.io/about/pricing).
- **Hugging Face Spaces (Docker):** compute Spaces now **require a paid plan**; only static Spaces are free. Source: [Spaces overview](https://huggingface.co/docs/hub/spaces-overview).
- **Koyeb:** the pricing page shows no free compute tier today (free Postgres only).

### If FalkorDB Cloud Free is the only option for the database

- **Verified:** **100 MB RAM / 100 MB dataset**, **stopped after 1 day without use, deleted after 7 days**, **no persistence or backups**, no card. Source: [FalkorDB Cloud free tier](https://docs.falkordb.com/cloud/free-tier.html).
- **Health-ping plan:** the app's `/api/health` runs `MATCH (n) RETURN count(n)` on every call, which counts as use. Schedule a ping every 6 hours (GitHub Actions cron `0 */6 * * *` doing `curl https://<app>/api/health`, or cron-job.org) so the instance is never idle for a day. If it is deleted anyway, create a new free instance, set `FALKORDB_HOST/PORT` (and password if any) on the app, redeploy: auto-seed restores the demo; agent memory is lost.

## Recommendation

Option A for the submission (always on, both containers, memory persists), with the Render setup as a 20-minute fallback if Oracle has no Arm capacity. The Groq/Gemini keys go in as environment variables on the host; **deterministic mode needs none**, and the deployed demo defaults to deterministic so judges cannot exhaust the free LLM quota.
