"""
intel_collector — Catalyst Job Function
=======================================
Runs on a schedule (daily at 08:00 UTC via Catalyst Job Scheduling).

What it does, in order:
  1. SCRAPE   — reads the RSS feed of every competitor blog
  2. DEDUPE   — skips any post whose URL is already in the Data Store
  3. ENRICH   — asks Claude AI to classify each NEW post into structured fields
  4. INSERT   — writes the enriched rows into the COMPETITOR_POSTS table

It writes NOTHING to chat. The website reads the Data Store instead.
"""

# ── Standard library imports ──────────────────────────────────────────────────
import os         # Reads environment variables (where our API key lives)
import json       # Converts the AI's JSON text reply into a Python dictionary
import calendar   # Converts RSS time tuples into Unix timestamps
import datetime   # Date/time maths — used for the lookback window

# ── Third-party imports ───────────────────────────────────────────────────────
import feedparser          # Downloads and parses RSS/Atom XML feeds
import anthropic           # Official Anthropic SDK — talks to Claude AI
import zcatalyst_sdk       # Zoho Catalyst SDK — gives us access to the Data Store


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

# The Data Store table we write into. Must already exist in the Catalyst console.
TABLE_NAME = "COMPETITOR_POSTS"

# How far back to look when scraping. We use 30 days (not 24 hours) because
# deduplication-by-URL is what prevents duplicates now — not a narrow time window.
# A wide window simply means the site backfills a month of history on its first run.
LOOKBACK_DAYS = 30

# The Claude model. Haiku is the fast, inexpensive tier — ideal for short
# classification tasks that run unattended every day.
AI_MODEL = "claude-haiku-4-5"

# The five competitor blogs we monitor, as RSS feed URLs.
# RSS is a standard XML format blogs publish so tools can check for new posts
# automatically, without scraping fragile HTML.
COMPETITOR_FEEDS = [
    {"name": "WhatsApp", "url": "https://blog.whatsapp.com/rss.xml"},
    {"name": "Signal",   "url": "https://signal.org/blog/rss.xml"},
    {"name": "Telegram", "url": "https://telegram.org/blog/rss.xml"},
    {"name": "Viber",    "url": "https://www.viber.com/en/blog/feed/"},
    {"name": "Line",     "url": "https://blog.line.me/en/feed"},
]

# The fixed set of themes the AI is allowed to choose from.
# Constraining the vocabulary is what makes the website's theme filter usable —
# if the AI invented a new theme every time, the filter list would be endless.
ALLOWED_THEMES = [
    "Privacy", "Security", "Monetization", "AI",
    "Payments", "Groups", "Platform", "Other",
]

# The fixed set of urgency levels, for the same reason.
ALLOWED_URGENCY = ["HIGH", "MEDIUM", "LOW"]


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — SCRAPE
# ══════════════════════════════════════════════════════════════════════════════

def scrape_feeds():
    """
    Downloads every competitor RSS feed and returns the posts published
    within the last LOOKBACK_DAYS days.

    Returns:
        A list of dicts, each with: competitor, title, url, published_at, snippet
    """
    # Work out the oldest publish date we still care about.
    # timezone.utc makes this comparison timezone-safe — RSS dates are in UTC.
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=LOOKBACK_DAYS)

    scraped = []

    for feed in COMPETITOR_FEEDS:
        name = feed["name"]
        url = feed["url"]
        print(f"[scrape] {name} -> {url}")

        try:
            # feedparser.parse() downloads the XML and turns it into an object
            # with a .entries list, where each entry is one blog post.
            parsed = feedparser.parse(url)

            # feedparser sets .bozo = True when the XML was malformed.
            # We only warn — a slightly broken feed is often still readable.
            if parsed.bozo:
                print(f"[scrape]   warning: malformed feed ({parsed.bozo_exception})")

            kept = 0
            for entry in parsed.entries:
                # 'published_parsed' is a time.struct_time tuple in UTC.
                # Some feeds omit it; we cannot date-filter those, so we skip them.
                published_parsed = getattr(entry, "published_parsed", None)
                if published_parsed is None:
                    continue

                # calendar.timegm() treats the tuple as UTC and returns a Unix
                # timestamp. fromtimestamp() then rebuilds it as a real datetime.
                published_at = datetime.datetime.fromtimestamp(
                    calendar.timegm(published_parsed),
                    tz=datetime.timezone.utc,
                )

                # Too old — ignore it.
                if published_at < cutoff:
                    continue

                link = entry.get("link", "").strip()
                if not link:
                    # Without a URL we cannot deduplicate, so the post is unusable.
                    continue

                scraped.append({
                    "competitor": name,
                    "title": entry.get("title", "Untitled").strip(),
                    "url": link,
                    "published_at": published_at,
                    # The raw RSS summary, trimmed. This is what we send to the AI.
                    "snippet": entry.get("summary", "")[:1500],
                })
                kept += 1

            print(f"[scrape]   {kept} post(s) within the last {LOOKBACK_DAYS} days")

        except Exception as error:
            # One broken feed must never abort the whole run — log and continue
            # so the other four competitors still get collected.
            print(f"[scrape]   ERROR for {name}: {error}")

    print(f"[scrape] total scraped: {len(scraped)}")
    return scraped


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — DEDUPE
# ══════════════════════════════════════════════════════════════════════════════

def get_existing_urls(app):
    """
    Fetches every URL already stored in the Data Store, so we can skip posts
    we have already processed (and avoid paying for duplicate AI calls).

    We do this with ONE query rather than one query per post.

    Args:
        app: the initialised Catalyst app object.

    Returns:
        A Python set of URL strings. Sets are used because checking
        "is this URL in the set?" is near-instant regardless of size.
    """
    # ZCQL is Catalyst's SQL-like query language for the Data Store.
    zcql = app.zcql()
    urls = set()

    # ZCQL caps results at 300 rows per query, so we page through using
    # LIMIT <offset>, <count> until a page comes back short.
    offset = 0
    page_size = 300

    while True:
        query = f"SELECT url FROM {TABLE_NAME} LIMIT {offset}, {page_size}"
        rows = zcql.execute_query(query)

        if not rows:
            break

        for row in rows:
            # ZCQL nests results under the table name, e.g. {"COMPETITOR_POSTS": {"url": ...}}
            urls.add(row[TABLE_NAME]["url"])

        # A short page means we have reached the end.
        if len(rows) < page_size:
            break
        offset += page_size

    print(f"[dedupe] {len(urls)} post(s) already stored")
    return urls


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — AI ENRICHMENT
# ══════════════════════════════════════════════════════════════════════════════

# The system prompt sets Claude's role and output contract for every call.
# Note how different this is from the old version: we are no longer asking for a
# readable briefing, we are asking for machine-readable FIELDS, because the
# website needs to filter and search on them.
SYSTEM_PROMPT = f"""You are a competitive intelligence analyst at a messaging app company.
You will be given ONE blog post from a competitor. Classify it.

Reply with ONLY a JSON object, no prose and no markdown fences, with exactly these keys:

{{
  "summary":        "1-2 sentences: what they actually shipped, announced, or changed.",
  "why_it_matters": "1 sentence: the implication for us as a competing messaging app.",
  "urgency":        "one of {ALLOWED_URGENCY}",
  "theme":          "one of {ALLOWED_THEMES}"
}}

Urgency guidance:
- HIGH   = a feature that directly threatens our differentiation, or a major platform shift.
- MEDIUM = a notable product change worth tracking.
- LOW    = minor updates, PR, partnerships, hiring, or company news.

Be concise and factual. Do not speculate beyond what the post says."""


def enrich_post(client, post):
    """
    Sends ONE post to Claude and gets back the four structured fields.

    Args:
        client: the Anthropic SDK client.
        post:   a dict from scrape_feeds().

    Returns:
        A dict with summary, why_it_matters, urgency, theme.
        Falls back to safe defaults if the AI call or JSON parse fails,
        so a single bad post never breaks the whole run.
    """
    # The user message carries the actual data for Claude to classify.
    user_message = (
        f"Competitor: {post['competitor']}\n"
        f"Title: {post['title']}\n"
        f"URL: {post['url']}\n"
        f"Content: {post['snippet']}"
    )

    try:
        # messages.create() sends one request and waits for the complete reply.
        response = client.messages.create(
            model=AI_MODEL,
            max_tokens=500,        # Plenty for four short fields; caps our cost
            system=SYSTEM_PROMPT,  # The role + output contract defined above
            messages=[
                {"role": "user", "content": user_message},
                # This is an "assistant prefill": we put the opening brace of the
                # JSON object into Claude's mouth. It guarantees the reply starts
                # as JSON instead of "Sure! Here's the JSON:".
                {"role": "assistant", "content": "{"},
            ],
        )

        # Because we prefilled "{", Claude's reply continues from there —
        # so we glue the brace back on to get a complete JSON document.
        raw = "{" + response.content[0].text
        data = json.loads(raw)

        # Never trust the model blindly: if it invents a theme or urgency outside
        # our allowed list, the website's filters would break. So we validate.
        urgency = data.get("urgency", "LOW").upper()
        if urgency not in ALLOWED_URGENCY:
            urgency = "LOW"

        theme = data.get("theme", "Other")
        if theme not in ALLOWED_THEMES:
            theme = "Other"

        return {
            "summary": data.get("summary", "")[:2000],
            "why_it_matters": data.get("why_it_matters", "")[:2000],
            "urgency": urgency,
            "theme": theme,
        }

    except Exception as error:
        # AI failed or returned unparseable text. Store the post anyway with
        # neutral values — a post in the feed without a summary is far better
        # than a crashed job and no data at all.
        print(f"[enrich]   ERROR for '{post['title']}': {error}")
        return {
            "summary": "",
            "why_it_matters": "",
            "urgency": "LOW",
            "theme": "Other",
        }


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — INSERT
# ══════════════════════════════════════════════════════════════════════════════

def insert_posts(app, rows):
    """
    Writes the enriched rows into the Data Store.

    Args:
        app:  the initialised Catalyst app object.
        rows: list of dicts matching the COMPETITOR_POSTS column names.
    """
    if not rows:
        print("[insert] nothing new to insert")
        return

    # datastore() gives us the Data Store component; .table() picks our table.
    table = app.datastore().table(TABLE_NAME)

    # insert_rows() accepts a batch, but Catalyst caps it at 100 rows per call,
    # so we slice the list into chunks of 100 and insert them one chunk at a time.
    chunk_size = 100
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start:start + chunk_size]
        table.insert_rows(chunk)
        print(f"[insert] wrote {len(chunk)} row(s)")

    print(f"[insert] total inserted: {len(rows)}")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def handler(job_details, context):
    """
    The function Catalyst calls when the scheduled job fires.

    Args:
        job_details: metadata about this job run, supplied by Job Scheduling.
        context:     lets us tell Catalyst the job succeeded or failed.
    """
    print("=" * 60)
    print("intel_collector — starting")
    print("=" * 60)

    try:
        # initialize() bootstraps the SDK using the credentials Catalyst
        # injects into the function's environment automatically.
        app = zcatalyst_sdk.initialize()

        # The Anthropic client reads ANTHROPIC_API_KEY from the environment,
        # which we set as a Catalyst environment variable in the console.
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")
        ai_client = anthropic.Anthropic()

        # 1. Scrape every feed.
        scraped = scrape_feeds()

        # 2. Drop anything we have seen before.
        existing_urls = get_existing_urls(app)
        new_posts = [p for p in scraped if p["url"] not in existing_urls]
        print(f"[dedupe] {len(new_posts)} genuinely new post(s) to enrich")

        # 3. Enrich each new post with AI, and build the Data Store rows.
        now = datetime.datetime.now(datetime.timezone.utc)
        rows = []
        for post in new_posts:
            print(f"[enrich] {post['competitor']}: {post['title'][:60]}")
            enriched = enrich_post(ai_client, post)

            rows.append({
                "competitor": post["competitor"],
                "title": post["title"][:500],
                "url": post["url"][:1000],
                # Catalyst expects datetimes as 'YYYY-MM-DD HH:MM:SS' strings.
                "published_at": post["published_at"].strftime("%Y-%m-%d %H:%M:%S"),
                "fetched_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "summary": enriched["summary"],
                "why_it_matters": enriched["why_it_matters"],
                "urgency": enriched["urgency"],
                "theme": enriched["theme"],
            })

        # 4. Save everything.
        insert_posts(app, rows)

        print("intel_collector — finished successfully")
        # Tells Job Scheduling this run succeeded (shown in the console).
        context.close_with_success()

    except Exception as error:
        # Any unhandled failure is reported to Catalyst so the run shows as
        # failed in the console instead of silently appearing to have worked.
        print(f"intel_collector — FAILED: {error}")
        context.close_with_failure()
