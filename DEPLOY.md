# Deployment

Two services: the FastAPI backend (Railway) and the dashboard (see the frontend
note below). They talk cross-origin, so both the API's `CORS_ALLOW_ORIGINS` and
the dashboard's `NEXT_PUBLIC_API_BASE` must name each other.

## The Upstox token: unattended paper-market data

Use an Upstox **Analytics Token** for this paper-only application. It is
read-only, lasts for one year, and supports the market-data and WebSocket APIs
used by the strategy. Generate it once under **Upstox Developer Apps →
Analytics**, store it in the backend as `UPSTOX_ANALYTICS_TOKEN`, and rotate it
before its displayed expiry date. Do not commit it.

No static IP is required for the market quote, historical-data, market
information, or WebSocket APIs used here. Static IP registration is only needed
for the Analytics Token's supported account-specific read APIs, which the paper
engine does not need.

The standard OAuth flow remains an optional fallback. Those access tokens expire
daily at ~03:30 IST and Upstox does not expose a refresh-token grant. Configure
the following only if you want that fallback:

| Variable | Value |
| --- | --- |
| `UPSTOX_API_KEY` | your Upstox app's API key |
| `UPSTOX_API_SECRET` | your Upstox app's API secret |
| `UPSTOX_REDIRECT_URI` | `https://<your-railway-app>.up.railway.app/auth/upstox/callback` |

Set that same callback URL as the redirect URI in your Upstox developer app.
To issue a daily token:

1. Open the dashboard's **Reports** tab and click **Refresh token** (or hit
   `GET /auth/upstox/login-url` and open the URL).
2. Log into Upstox. It redirects to `/auth/upstox/callback`, which exchanges the
   code for a token, stores it, and shows a confirmation page.

The OAuth token is then live - no redeploy or env-var edit. It is persisted on the
research database (the Railway volume), so a restart keeps it, and every run
reads the current token, so the next scheduled run picks it up. `GET
/auth/upstox/status` reports whether the token is present and still within its
03:30 window; `GET /health` includes the same. If it is stale at 09:15 the
scheduler cleanly marks the day's slots `SKIPPED (market data unavailable)`.

You can also set a token directly with `PUT /auth/upstox/token` (body
`{"access_token": "..."}`) if you already hold one, and `UPSTOX_ACCESS_TOKEN`
still seeds the store on first boot for a quick start.

Do not automate the Upstox login form or store Upstox passwords/TOTP secrets.
Use the Analytics Token instead for fully unattended paper runs.

## Backend on Railway

The repo ships a `Dockerfile` and `railway.json`.

1. **New Railway project -> Deploy from repo.** It builds the Dockerfile.
2. **Attach a Volume** and mount it at `/data`. SQLite is the whole database;
   without a volume every redeploy wipes your runs, observations, and reports.
   The image defaults `PAPER_DB_PATH=/data/paper_trading.db`.
3. **Set variables:**

   | Variable | Value |
   | --- | --- |
   | `PAPER_DB_PATH` | `/data/paper_trading.db` |
   | `UPSTOX_ANALYTICS_TOKEN` | preferred read-only, one-year token for unattended paper runs |
   | `UPSTOX_API_KEY` / `UPSTOX_API_SECRET` | optional standard OAuth fallback credentials |
   | `UPSTOX_REDIRECT_URI` | optional OAuth fallback callback: `https://<app>.up.railway.app/auth/upstox/callback` |
   | `UPSTOX_ACCESS_TOKEN` | optional daily OAuth seed token |
   | `SCHEDULER_ENABLED` | `true` |
   | `SCHEDULER_MAX_POSITIONS` | `5` |
   | `SCHEDULER_ALLOCATION` | `100000` (per position) |
   | `SCHEDULER_INITIAL_CASH` | `600000` (>= max_positions x allocation) |
   | `SCHEDULER_COOLDOWN_SECONDS` | `900` (15-min re-entry cooldown) |
   | `CORS_ALLOW_ORIGINS` | your dashboard origin, e.g. `https://jupiter.pages.dev` |
   | `PAPER_INITIAL_CASH` | `100000` |

   Railway injects `$PORT`; the app binds `0.0.0.0:$PORT` automatically.
4. **Keep it to one replica.** `railway.json` sets `numReplicas: 1` and you must
   not raise it. The scheduler is a single in-process thread and SQLite is a
   single file - two replicas would launch every run twice and cannot share the
   database. Scale by running more arms per day, not more replicas.
5. Health check is `GET /health`.

Once up, the scheduler launches two full-session runs (5-second ticks and
1-minute bars) at 09:15 IST. They share one cached NIFTY 100 survey;
`GET /schedule/status` shows them, and after they settle past the
15:30 close the day's report appears at `GET /reports/daily/{date}` and in the
dashboard's Reports tab. Each run needs its account funded to cover
`max_positions x allocation`, which `SCHEDULER_INITIAL_CASH` provides.

## Frontend

**This dashboard is not a drop-in Vercel app.** It is built with `vinext` (the
OpenAI Sites Vite scaffold) and its production build targets **Cloudflare
Workers** - the build emits `wrangler.json` and `nodejs_compat` bindings, and
there is no static `index.html` for Vercel to serve. Deploying it unchanged to
Vercel will not work.

Two honest paths:

### Recommended: private OpenAI Sites / Cloudflare Worker

The dashboard contains `.openai/hosting.json` and a server-side `/api` proxy.
Deploy it as an owner-only Site and set the Site runtime variable
`JUPITER_BACKEND_URL` to the Railway backend URL. The browser then calls the
private Site on the same origin; the Worker forwards requests to Railway, so the
backend URL is not baked into the browser bundle and Railway CORS is not needed
for dashboard traffic.

### If you specifically want Vercel

The dashboard has to be moved off the vinext/Cloudflare scaffold first - either
to a standard Next.js app or to a plain Vite SPA with an `index.html` and a
client-side router. The page component is already `'use client'`, so the
migration is mostly mechanical (it makes no server calls of its own beyond
`fetch` to the API), but it is a code change, not a config file. Say the word
and it can be done as a separate step; until then, Cloudflare is the working
frontend host.

The optional `NEXT_PUBLIC_API_BASE` path remains available for split deployments
that intentionally call Railway directly from the browser; those deployments
must configure Railway `CORS_ALLOW_ORIGINS`.

## NSE holidays

The scheduler loads the official NSE holiday-master `CM` calendar by year and
caches it for 12 hours. It skips weekends and cash-market holidays before a plan
or run is created. The published 2026 calendar is bundled as an offline fallback;
for later years the scheduler fails closed if NSE is unreachable, then retries on
subsequent scheduler ticks. Inspect the applied dates at
`GET /schedule/calendar?year=2026`.

## Local development

```bash
# backend
UPSTOX_ANALYTICS_TOKEN=... SCHEDULER_ENABLED=false jupiter-api   # localhost:8000

# dashboard (proxies /api to localhost:8000 in dev)
cd dashboard && npm install && npm run dev                    # localhost:3000
```

Leave `SCHEDULER_ENABLED=false` locally unless you want the day's runs to fire on
your machine. You can always drive one manually from the Home tab, preview a
plan with `GET /schedule/plan`, advance the scheduler once with
`POST /schedule/tick`, or build a report with `POST /reports/daily/{date}/build`.
