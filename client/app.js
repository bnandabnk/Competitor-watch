/*
 * app.js — Competitor Watch frontend
 * ==================================
 * Responsibilities:
 *   1. Gate the page behind Catalyst Authentication
 *   2. Build the sidebar filters from GET /filters
 *   3. Query GET /posts whenever a filter or the search box changes
 *   4. Render the results as cards
 */

// ── CONFIG ───────────────────────────────────────────────────────────────────
// Catalyst exposes Advanced I/O functions at /server/<function-name>/<route>.
const API_BASE = "/server/intel_api";

// How many posts to request per page.
const PAGE_SIZE = 25;

// Brand colours for the competitor badge, so you can identify a source at a glance.
const BRAND_COLORS = {
  WhatsApp: "#25D366",
  Signal:   "#3A76F0",
  Telegram: "#2AABEE",
  Viber:    "#7360F2",
  Line:     "#06C755",
};

// Emoji for each urgency level, matching the AI's classification.
const URGENCY_ICONS = { HIGH: "🔴", MEDIUM: "🟡", LOW: "🟢" };

// ── STATE ────────────────────────────────────────────────────────────────────
// A single object holding the current filter selections. Keeping all state in
// one place means "re-run the query" is always the same function call.
const state = {
  competitor: new Set(),  // Sets make add/remove/has straightforward
  urgency:    new Set(),
  theme:      new Set(),
  q:          "",
  days:       "30",
  offset:     0,
};


// ══════════════════════════════════════════════════════════════════════════════
// 1. AUTHENTICATION GATE
// ══════════════════════════════════════════════════════════════════════════════

/*
 * Checks whether the visitor is signed in before showing anything.
 * catalyst.auth.isUserAuthenticated() returns a Promise that resolves when the
 * user has a valid session and rejects when they do not.
 */
async function checkAuth() {
  // Guard: if the SDK scripts failed to load, `catalyst` is undefined.
  // Without this check we would blindly redirect to the login URL and the
  // user would just see a bare 404 with no clue what went wrong.
  if (typeof catalyst === "undefined" || !catalyst.auth) {
    showFatal(
      "The Catalyst SDK did not load. Check that both SDK script tags are " +
      "present in index.html and that this page is served by Catalyst " +
      "(catalyst serve / catalyst deploy) rather than opened as a local file."
    );
    return;
  }

  try {
    await catalyst.auth.isUserAuthenticated();

    // Signed in — hide the loading gate and reveal the app.
    document.getElementById("auth-check").hidden = true;
    document.getElementById("app").hidden = false;

    // Now that we know we can call the API, start loading data.
    await loadFilters();
    await loadPosts({ reset: true });

  } catch (err) {
    // Not signed in. /__catalyst/auth/login is Catalyst's own hosted login
    // page — but it only exists once Authentication is enabled for the
    // project in the console. If it is not, this redirect lands on a 404,
    // so we say so explicitly rather than leaving a dead end.
    console.warn("Not authenticated:", err);
    showFatal(
      "You are not signed in. Redirecting to the login page… " +
      "If you land on a 404, Authentication has not been enabled for this " +
      "Catalyst project yet (console - Authentication - set up Hosted Login)."
    );
    // Small delay so the message is readable if the redirect does 404.
    setTimeout(() => { window.location.href = "/__catalyst/auth/login"; }, 1500);
  }
}

/*
 * Replaces the loading gate with an error message.
 * Used for failures that happen before the app can render at all.
 */
function showFatal(message) {
  const gate = document.getElementById("auth-check");
  gate.innerHTML = "";

  const box = document.createElement("div");
  box.className = "notice error";
  box.style.maxWidth = "32rem";
  box.textContent = message;

  gate.appendChild(box);
  gate.hidden = false;
}


// ══════════════════════════════════════════════════════════════════════════════
// 2. SIDEBAR FILTERS
// ══════════════════════════════════════════════════════════════════════════════

/*
 * Asks the API which filter values exist in the data, then renders a checkbox
 * for each one. Building this from real data means the sidebar never offers a
 * filter that would return zero results.
 */
async function loadFilters() {
  try {
    // fetch() makes an HTTP request and returns a Promise.
    const response = await fetch(`${API_BASE}/filters`);
    const data = await response.json();

    renderCheckboxes("competitor-filters", "competitor", data.competitor || []);
    renderCheckboxes("urgency-filters",    "urgency",    data.urgency    || []);
    renderCheckboxes("theme-filters",      "theme",      data.theme      || []);

  } catch (err) {
    console.error("Could not load filters:", err);
  }
}

/*
 * Renders one list of checkboxes into a container.
 *
 * @param containerId  the id of the <div> to fill
 * @param stateKey     which Set in `state` this list controls
 * @param values       [{ value, count }] from the API
 */
function renderCheckboxes(containerId, stateKey, values) {
  const container = document.getElementById(containerId);
  container.innerHTML = "";  // clear anything rendered previously

  values.forEach(({ value, count }) => {
    const label = document.createElement("label");
    label.className = "checkbox-row";

    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = value;

    // When a box is ticked, add its value to the matching Set and re-query.
    // We reset to offset 0 because the result list is now a different list.
    input.addEventListener("change", () => {
      if (input.checked) state[stateKey].add(value);
      else state[stateKey].delete(value);
      loadPosts({ reset: true });
    });

    const text = document.createElement("span");
    text.textContent = value;

    const badge = document.createElement("span");
    badge.className = "count";
    badge.textContent = count;

    label.append(input, text, badge);
    container.appendChild(label);
  });
}


// ══════════════════════════════════════════════════════════════════════════════
// 3. LOADING POSTS
// ══════════════════════════════════════════════════════════════════════════════

/*
 * Builds the query string from `state` and fetches a page of posts.
 *
 * @param reset  true  = this is a new search, replace the feed
 *               false = this is "Load more", append to the feed
 */
async function loadPosts({ reset }) {
  if (reset) state.offset = 0;

  // URLSearchParams builds a properly URL-encoded query string for us,
  // so a search term containing spaces or "&" cannot break the request.
  const params = new URLSearchParams();

  // append() (not set()) lets the same key appear multiple times, which is how
  // we send several competitors at once: ?competitor=Signal&competitor=Line
  state.competitor.forEach(v => params.append("competitor", v));
  state.urgency.forEach(v => params.append("urgency", v));
  state.theme.forEach(v => params.append("theme", v));

  if (state.q)    params.set("q", state.q);
  if (state.days) params.set("days", state.days);
  params.set("limit", PAGE_SIZE);
  params.set("offset", state.offset);

  const errorBox = document.getElementById("error-state");
  errorBox.hidden = true;

  try {
    const response = await fetch(`${API_BASE}/posts?${params.toString()}`);
    const data = await response.json();

    if (!response.ok) throw new Error(data.error || "Request failed");

    renderPosts(data.posts, { reset });
    updateCount(data.total, state.offset + data.posts.length);

    // Show "Load more" only while there are still unseen results.
    state.offset += data.posts.length;
    document.getElementById("load-more").hidden = state.offset >= data.total;

  } catch (err) {
    // Show the failure instead of leaving a silently blank page.
    errorBox.textContent = `Could not load posts: ${err.message}`;
    errorBox.hidden = false;
  }
}

/*
 * Renders post cards into the feed.
 */
function renderPosts(posts, { reset }) {
  const feed = document.getElementById("feed");
  if (reset) feed.innerHTML = "";

  // Show the empty state only when a fresh search returned nothing.
  document.getElementById("empty-state").hidden = !(reset && posts.length === 0);

  posts.forEach(post => feed.appendChild(buildCard(post)));
}

/*
 * Builds the DOM for one post card.
 *
 * We create elements with createElement and set .textContent rather than
 * assembling an HTML string. textContent escapes its input, so a post title
 * containing HTML can never inject markup into our page.
 */
function buildCard(post) {
  const card = document.createElement("article");
  card.className = "card";

  // ── Top row: competitor badge, urgency pill, date ──────────────────────────
  const meta = document.createElement("div");
  meta.className = "card-meta";

  const badge = document.createElement("span");
  badge.className = "badge";
  badge.textContent = post.competitor;
  // Fall back to grey if we meet a competitor with no colour defined.
  badge.style.backgroundColor = BRAND_COLORS[post.competitor] || "#64748b";

  const urgency = document.createElement("span");
  urgency.className = "urgency";
  urgency.textContent = `${URGENCY_ICONS[post.urgency] || ""} ${post.urgency}`;

  const theme = document.createElement("span");
  theme.className = "theme";
  theme.textContent = post.theme;

  const date = document.createElement("span");
  date.className = "date";
  date.textContent = formatDate(post.published_at);

  meta.append(badge, urgency, theme, date);

  // ── Title, linking out to the original post ────────────────────────────────
  const title = document.createElement("a");
  title.className = "card-title";
  title.href = post.url;
  title.target = "_blank";                 // open in a new tab
  title.rel = "noopener noreferrer";       // security: stops the opened page
                                           // from controlling our tab
  title.textContent = post.title;

  card.append(meta, title);

  // ── AI summary (only rendered if the AI produced one) ──────────────────────
  if (post.summary) {
    const summary = document.createElement("p");
    summary.className = "card-summary";
    summary.textContent = post.summary;
    card.appendChild(summary);
  }

  // ── "Why it matters" line ──────────────────────────────────────────────────
  if (post.why_it_matters) {
    const why = document.createElement("p");
    why.className = "card-why";

    const label = document.createElement("strong");
    label.textContent = "Why it matters: ";

    why.append(label, document.createTextNode(post.why_it_matters));
    card.appendChild(why);
  }

  return card;
}

/*
 * Turns the stored "2026-11-03 08:15:00" string into something readable.
 */
function formatDate(value) {
  if (!value) return "";
  // Replacing the space with "T" makes the string valid ISO 8601 so that
  // every browser parses it the same way.
  const date = new Date(value.replace(" ", "T") + "Z");
  if (isNaN(date)) return value;  // unparseable — show the raw value
  return date.toLocaleDateString(undefined, {
    year: "numeric", month: "short", day: "numeric",
  });
}

/*
 * Updates the "Showing 25 of 318 posts" line in the header.
 */
function updateCount(total, shown) {
  const el = document.getElementById("result-count");
  el.textContent = total === 0
    ? "No posts"
    : `Showing ${shown} of ${total} post${total === 1 ? "" : "s"}`;
}


// ══════════════════════════════════════════════════════════════════════════════
// 4. EVENT WIRING
// ══════════════════════════════════════════════════════════════════════════════

// ── Search box, debounced ─────────────────────────────────────────────────────
// Without debouncing, typing "encryption" would fire 10 API calls. We instead
// wait until the user has paused for 350ms, then send one request.
let searchTimer;
document.getElementById("search-input").addEventListener("input", (event) => {
  clearTimeout(searchTimer);              // cancel the previously queued call
  searchTimer = setTimeout(() => {
    state.q = event.target.value.trim();
    loadPosts({ reset: true });
  }, 350);
});

// ── Time range dropdown ───────────────────────────────────────────────────────
document.getElementById("days-select").addEventListener("change", (event) => {
  state.days = event.target.value;
  loadPosts({ reset: true });
});

// ── Load more ─────────────────────────────────────────────────────────────────
// reset:false means the next page is appended to the existing list.
document.getElementById("load-more").addEventListener("click", () => {
  loadPosts({ reset: false });
});

// ── Clear all filters ─────────────────────────────────────────────────────────
document.getElementById("clear-filters").addEventListener("click", () => {
  state.competitor.clear();
  state.urgency.clear();
  state.theme.clear();
  state.q = "";
  state.days = "30";

  // Reset the visible controls to match the cleared state.
  document.querySelectorAll(".sidebar input[type=checkbox]")
    .forEach(box => { box.checked = false; });
  document.getElementById("search-input").value = "";
  document.getElementById("days-select").value = "30";

  loadPosts({ reset: true });
});

// ── Sign out ──────────────────────────────────────────────────────────────────
document.getElementById("logout-btn").addEventListener("click", () => {
  // signOut() takes the URL to land on after the session is destroyed.
  catalyst.auth.signOut(window.location.origin);
});


// ── START ─────────────────────────────────────────────────────────────────────
// Everything begins with the authentication check.
checkAuth();
