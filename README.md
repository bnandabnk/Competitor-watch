# Competitor Watch

A standalone site that tracks product updates from **WhatsApp, Signal, Telegram, Viber
and Line** in one place, built on [Zoho Catalyst](https://catalyst.zoho.com).

Every morning a scheduled job reads each competitor's blog RSS feed, asks Claude AI to
classify any new post, and stores it. The site then lets the PM team browse, filter and
search the whole history.

---

## How it works

```
┌──────────────────┐   daily 08:00 UTC   ┌────────────────────────┐
│  Job Scheduling  │────────────────────▶│  intel_collector       │
└──────────────────┘                     │  scrape → AI → store   │
                                         └───────────┬────────────┘
                                                     │
                                                     ▼
                                         ┌────────────────────────┐
                                         │  Data Store            │
                                         │  COMPETITOR_POSTS      │
                                         └───────────┬────────────┘
                                                     │ ZCQL
                                                     ▼
┌──────────────────┐    filtered feed    ┌────────────────────────┐
│  client/         │◀───────────────────▶│  intel_api  (Flask)    │
│  the website     │                     └────────────────────────┘
└──────────────────┘
        ▲
        │ hosted login
┌──────────────────┐
│ Catalyst Auth    │
└──────────────────┘
```

| Piece | What it is | What it does |
|---|---|---|
| `functions/intel_collector` | Job Function (Python) | Scrapes the 5 RSS feeds, dedupes by URL, classifies new posts with Claude, writes to the Data Store |
| `functions/intel_api` | Advanced I/O Function (Python + Flask) | REST API the site calls — `GET /posts` and `GET /filters` |
| `client/` | Web Client Hosting | The site itself: feed, filters, search. No build step |

---

## What the AI adds to each post

The collector doesn't just store the blog post — it asks Claude to classify it into
four fields, which is what makes the site filterable:

| Field | Example |
|---|---|
| `summary` | "Signal rolled out usernames, letting users connect without sharing a phone number." |
| `why_it_matters` | "Removes the phone-number requirement that has been a key onboarding barrier for them." |
| `urgency` | `HIGH` / `MEDIUM` / `LOW` |
| `theme` | `Privacy` / `Security` / `Monetization` / `AI` / `Payments` / `Groups` / `Platform` / `Other` |

Model used is `claude-haiku-4-5` — the fast, low-cost tier. At roughly 5–15 new posts a
day this runs to well under $0.10/month.

---

## Setup

### 1. Install the Catalyst CLI

```bash
npm install -g zcatalyst-cli
```

```bash
catalyst login
```

### 2. Link this folder to a Catalyst project

```bash
catalyst init
```

Pick **Functions** and **Client** when prompted. This creates `.catalystrc`
(gitignored — it's specific to your machine).

### 3. Create the Data Store table

In the Catalyst console → **Data Store** → create a table named `COMPETITOR_POSTS`
with these columns:

| Column | Type | Notes |
|---|---|---|
| `competitor` | varchar(50) | |
| `title` | varchar(500) | |
| `url` | varchar(1000) | **mark as unique** — this is the dedupe key |
| `published_at` | datetime | |
| `fetched_at` | datetime | |
| `summary` | text | |
| `why_it_matters` | text | |
| `urgency` | varchar(10) | |
| `theme` | varchar(50) | |

The unique constraint on `url` is what makes the collector safe to re-run — a second
run inserts only genuinely new posts.

### 4. Add your Anthropic API key

Get a key at [console.anthropic.com](https://console.anthropic.com/settings/keys) →
**Settings → API Keys → Create Key**. It starts with `sk-ant-api03-` and is shown
only once.

In the Catalyst console → **Settings → Environment Variables**, add:

```
ANTHROPIC_API_KEY = sk-ant-api03-...
```

Catalyst injects this into the function at runtime, so the key is never in the repo.

### 5. Enable authentication

Catalyst console → **Authentication** → enable it, then invite your team's email
addresses. Only invited users can open the site.

### 6. Deploy

```bash
catalyst deploy
```

### 7. Schedule the daily run

Catalyst console → **Job Scheduling** → create a Job Pool, then a job targeting the
`intel_collector` function with the cron expression:

```
0 8 * * *
```

That's 08:00 UTC daily. (Catalyst's older *Cron* component reached end-of-life on
30 Apr 2026 — Job Scheduling is its replacement.)

---

## Local development

```bash
catalyst serve
```

Serves the client and functions at `http://localhost:3000`.

To run the collector by hand without waiting for the schedule:

```bash
catalyst functions:shell
```

Then invoke `intel_collector`. Run it **twice** — the second run should insert zero
rows, which proves deduplication is working.

---

## API reference

Both endpoints require an authenticated Catalyst session.

### `GET /server/intel_api/posts`

| Parameter | Example | Effect |
|---|---|---|
| `competitor` | `Signal` | Exact match. Repeatable: `?competitor=Signal&competitor=Line` |
| `urgency` | `HIGH` | Exact match. Repeatable |
| `theme` | `Privacy` | Exact match. Repeatable |
| `q` | `encryption` | Keyword search across title + summary |
| `days` | `7` | Only posts newer than N days |
| `limit` | `25` | Page size (max 100) |
| `offset` | `0` | Pagination offset |

```json
{
  "posts": [ { "competitor": "Signal", "title": "...", "urgency": "HIGH", ... } ],
  "total": 318,
  "limit": 25,
  "offset": 0
}
```

### `GET /server/intel_api/filters`

Returns the distinct values present in the data with counts, so the sidebar only
offers filters that would actually return results.

---

## Adding a competitor

Edit `COMPETITOR_FEEDS` in [`functions/intel_collector/main.py`](functions/intel_collector/main.py),
and add the name to `ALLOWED_COMPETITORS` in
[`functions/intel_api/main.py`](functions/intel_api/main.py) and `BRAND_COLORS` in
[`client/app.js`](client/app.js). Then redeploy.

---

## Not built yet

The `theme` and `urgency` columns are populated from day one specifically so these
need no schema change later:

- **Trends dashboard** — posts per competitor over time, theme heatmap, urgency mix
- **Daily briefing archive** — browse the day's exec summary by date
- **Run-now button + feed health page** — per-feed last-success and scrape errors
- **Per-user bookmarks / read state**
- **SmartBrowz fallback** for competitors whose RSS feed breaks
- **CSV export** of a filtered view
