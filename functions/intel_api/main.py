"""
intel_api — Catalyst Advanced I/O Function
==========================================
The REST API that the website calls to read competitor posts.

An "Advanced I/O" function is Catalyst's way of running a normal web framework
(here: Flask) inside a serverless function. Catalyst hands incoming HTTP
requests to our Flask app and sends our responses back to the browser.

Endpoints:
  GET /posts    — the filtered, paginated feed
  GET /filters  — the distinct filter values and their counts, for the sidebar
"""

# ── Standard library imports ──────────────────────────────────────────────────
import datetime   # Used to turn "last 7 days" into a real cut-off date

# ── Third-party imports ───────────────────────────────────────────────────────
from flask import Flask, request, jsonify  # Flask = the web framework
import zcatalyst_sdk                       # Catalyst SDK — Data Store access

# Flask(__name__) creates the web application object.
# Catalyst looks for a variable named `app` and routes requests to it.
app = Flask(__name__)

TABLE_NAME = "COMPETITOR_POSTS"

# Columns we return to the browser. Listing them explicitly (rather than SELECT *)
# means adding a column later never accidentally leaks it to the frontend.
POST_COLUMNS = [
    "ROWID", "competitor", "title", "url",
    "published_at", "summary", "why_it_matters", "urgency", "theme",
]

# Whitelists. Any filter value from the browser is checked against these before
# it goes anywhere near a query — this is what prevents query injection.
ALLOWED_COMPETITORS = ["WhatsApp", "Signal", "Telegram", "Viber", "Line"]
ALLOWED_URGENCY = ["HIGH", "MEDIUM", "LOW"]
ALLOWED_THEMES = [
    "Privacy", "Security", "Monetization", "AI",
    "Payments", "Groups", "Platform", "Other",
]


def escape_literal(value):
    """
    Makes a user-supplied string safe to embed in a ZCQL query.

    ZCQL uses single quotes around text values, so a value containing its own
    single quote would end the string early and let an attacker append their
    own query. Doubling every quote ("O'Brien" -> "O''Brien") neutralises that.
    """
    return str(value).replace("'", "''")


def build_where_clause(args):
    """
    Turns the browser's query-string parameters into a ZCQL WHERE clause.

    Only parameters that were actually supplied become conditions, so
    `/posts` with no filters returns everything.

    Args:
        args: Flask's request.args (the parsed query string).

    Returns:
        A WHERE clause string (without the word WHERE), or "" if no filters.
    """
    conditions = []

    # ── Competitor filter (repeatable: ?competitor=Signal&competitor=Line) ────
    # getlist() returns every occurrence of the parameter, not just the first.
    competitors = [c for c in args.getlist("competitor") if c in ALLOWED_COMPETITORS]
    if competitors:
        # Build an IN (...) list so several competitors can be selected at once.
        quoted = ", ".join(f"'{escape_literal(c)}'" for c in competitors)
        conditions.append(f"competitor IN ({quoted})")

    # ── Urgency filter (repeatable) ───────────────────────────────────────────
    urgencies = [u for u in args.getlist("urgency") if u in ALLOWED_URGENCY]
    if urgencies:
        quoted = ", ".join(f"'{escape_literal(u)}'" for u in urgencies)
        conditions.append(f"urgency IN ({quoted})")

    # ── Theme filter (repeatable) ─────────────────────────────────────────────
    themes = [t for t in args.getlist("theme") if t in ALLOWED_THEMES]
    if themes:
        quoted = ", ".join(f"'{escape_literal(t)}'" for t in themes)
        conditions.append(f"theme IN ({quoted})")

    # ── Keyword search ────────────────────────────────────────────────────────
    # LIKE with % wildcards on both sides means "contains this text anywhere".
    # We search the title and the AI summary together.
    search = args.get("q", "").strip()
    if search:
        safe = escape_literal(search)
        conditions.append(f"(title LIKE '%{safe}%' OR summary LIKE '%{safe}%')")

    # ── Date window ("last N days") ───────────────────────────────────────────
    days = args.get("days", "").strip()
    if days.isdigit() and int(days) > 0:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=int(days))
        conditions.append(f"published_at > '{cutoff.strftime('%Y-%m-%d %H:%M:%S')}'")

    # Join every condition with AND. No conditions means no WHERE clause at all.
    return " AND ".join(conditions)


@app.route("/posts", methods=["GET"])
def get_posts():
    """
    Returns the competitor feed, filtered and paginated.

    Query parameters (all optional):
      competitor, urgency, theme  — repeatable exact-match filters
      q                           — keyword search in title + summary
      days                        — only posts newer than N days
      limit, offset               — pagination
    """
    try:
        catalyst_app = zcatalyst_sdk.initialize()
        zcql = catalyst_app.zcql()

        where = build_where_clause(request.args)
        # Only prepend the WHERE keyword if we actually have conditions.
        where_sql = f" WHERE {where}" if where else ""

        # ── Pagination ────────────────────────────────────────────────────────
        # int(... or default) with a try/except guards against "?limit=banana".
        try:
            limit = min(int(request.args.get("limit", 25)), 100)  # cap at 100
        except ValueError:
            limit = 25
        try:
            offset = max(int(request.args.get("offset", 0)), 0)
        except ValueError:
            offset = 0

        # ── Total count (for "showing 25 of 318") ─────────────────────────────
        # Runs the same filters but just counts, so the frontend can show
        # how many results exist beyond the current page.
        count_rows = zcql.execute_query(
            f"SELECT COUNT(ROWID) AS total FROM {TABLE_NAME}{where_sql}"
        )
        total = int(count_rows[0][TABLE_NAME]["total"]) if count_rows else 0

        # ── The page of results ───────────────────────────────────────────────
        # ORDER BY published_at DESC puts the newest post first.
        columns = ", ".join(POST_COLUMNS)
        query = (
            f"SELECT {columns} FROM {TABLE_NAME}{where_sql} "
            f"ORDER BY published_at DESC LIMIT {offset}, {limit}"
        )
        rows = zcql.execute_query(query)

        # ZCQL nests each row under the table name, e.g.
        #   {"COMPETITOR_POSTS": {"title": "...", ...}}
        # We flatten that so the frontend gets a plain list of post objects.
        posts = [row[TABLE_NAME] for row in rows] if rows else []

        # jsonify() converts the Python dict to JSON and sets the
        # Content-Type: application/json header automatically.
        return jsonify({
            "posts": posts,
            "total": total,
            "limit": limit,
            "offset": offset,
        }), 200

    except Exception as error:
        # Return a real error the frontend can display, instead of a blank page.
        print(f"[/posts] ERROR: {error}")
        return jsonify({"error": str(error)}), 500


@app.route("/filters", methods=["GET"])
def get_filters():
    """
    Returns the distinct values present in the data, with counts.

    The sidebar uses this so it only offers filters that would actually
    return something — e.g. no "Payments" checkbox if no post is tagged Payments.
    """
    try:
        catalyst_app = zcatalyst_sdk.initialize()
        zcql = catalyst_app.zcql()

        result = {}

        # One GROUP BY query per filter dimension. GROUP BY collapses rows that
        # share a value and COUNT tells us how many were collapsed.
        for column in ["competitor", "urgency", "theme"]:
            rows = zcql.execute_query(
                f"SELECT {column}, COUNT(ROWID) AS count FROM {TABLE_NAME} "
                f"GROUP BY {column}"
            )
            result[column] = [
                {"value": row[TABLE_NAME][column], "count": int(row[TABLE_NAME]["count"])}
                for row in (rows or [])
                if row[TABLE_NAME].get(column)  # skip empty/null values
            ]

        return jsonify(result), 200

    except Exception as error:
        print(f"[/filters] ERROR: {error}")
        return jsonify({"error": str(error)}), 500


# When running locally via `catalyst serve`, this starts Flask's dev server.
# In production Catalyst runs the `app` object itself, so this block is skipped.
if __name__ == "__main__":
    app.run(debug=True)
