# Deployment

Two services: the FastAPI backend (Railway) and the dashboard (see the frontend
note below). They talk cross-origin, so both the API's `CORS_ALLOW_ORIGINS` and
the dashboard's `NEXT_PUBLIC_API_BASE` must name each other.

## The one hard blocker: the Upstox token

Upstox access tokens **expire every day** (around 03:30 IST) and can only be
refreshed through an interactive OAuth login with your API key and secret. Until
a fresh token is set each morning, every scheduled run fails at market-data
fetch and the scheduler marks the day's slots `SKIPPED (market data
unavailable)`. Unattended daily automation therefore needs one of:

- a small morning job that runs the Upstox OAuth exchange and writes the new
  `UPSTOX_ACCESS_TOKEN` to the Railway service (Upstox offers an extended-token
  flow for exactly this - worth setting up), or
- you pasting the token into Railway's variables each morning before 09:15 IST.

Nothing else in this stack can refresh it for you, because the login is
interactive and tied to your Upstox credentials. `GET /health` reports
`upstox_configured`, and `GET /schedule/status` shows whether slots are being
skipped, so you can tell at a glance whether today's token is live.

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
   | `UPSTOX_ACCESS_TOKEN` | today's token (see above) |
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

Once up, the scheduler launches four full-session runs (one per entry timeframe)
at 09:15 IST; `GET /schedule/status` shows them, and after they settle past the
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

### Recommended: Cloudflare (already configured)

```bash
cd dashboard
echo "NEXT_PUBLIC_API_BASE=https://<your-railway-app>.up.railway.app" > .env
npm install
npm run build
npx wrangler deploy      # or connect the repo in the Cloudflare dashboard
```

Set `NEXT_PUBLIC_API_BASE` to the Railway URL (baked in at build time, so
rebuild if it changes), and set the backend's `CORS_ALLOW_ORIGINS` to the
resulting Cloudflare URL.

### If you specifically want Vercel

The dashboard has to be moved off the vinext/Cloudflare scaffold first - either
to a standard Next.js app or to a plain Vite SPA with an `index.html` and a
client-side router. The page component is already `'use client'`, so the
migration is mostly mechanical (it makes no server calls of its own beyond
`fetch` to the API), but it is a code change, not a config file. Say the word
and it can be done as a separate step; until then, Cloudflare is the working
frontend host.

Either way the API base is `NEXT_PUBLIC_API_BASE` and the backend must list the
frontend origin in `CORS_ALLOW_ORIGINS`.

## NSE holidays

`is_trading_day` only skips weekends; it does not know NSE trading holidays. On a
holiday the scheduler will still build a plan and try to launch - the runs simply
find the market closed and record nothing tradeable. Add the holiday list to
`schedule.is_trading_day` if you want those days skipped cleanly.

## Local development

```bash
# backend
UPSTOX_ACCESS_TOKEN=... SCHEDULER_ENABLED=false jupiter-api   # localhost:8000

# dashboard (proxies /api to localhost:8000 in dev)
cd dashboard && npm install && npm run dev                    # localhost:3000
```

Leave `SCHEDULER_ENABLED=false` locally unless you want the day's runs to fire on
your machine. You can always drive one manually from the Home tab, preview a
plan with `GET /schedule/plan`, advance the scheduler once with
`POST /schedule/tick`, or build a report with `POST /reports/daily/{date}/build`.
