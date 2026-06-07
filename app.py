import json
import os
import signal
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from gauth import build_google_login_url, verify_google_oauth_callback
from expenses import load_expense_report, recover_archived_transactions, scan_expense_emails
from profiles import (
    list_profiles,
    migrate_legacy_token,
    resolve_profile_id_for_oauth,
    save_profile,
    sanitize_profile_id,
)

load_dotenv(override=True)
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

app = FastAPI(title="Hermes Google OAuth")

CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8010/auth/callback")
AUTO_KILL_AFTER_SECONDS = float(os.getenv("AUTO_KILL_AFTER_SECONDS", "0"))
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]
STATE_FILE = Path(".oauth_state.json")

_oauth_state: str | None = None
_oauth_profile_id: str | None = None


def _stop_self(delay_seconds: float = 0.2):
    time.sleep(delay_seconds)
    os.kill(os.getpid(), signal.SIGINT)


def _auto_kill_worker():
    time.sleep(AUTO_KILL_AFTER_SECONDS)
    os.kill(os.getpid(), signal.SIGINT)


@app.on_event("startup")
def _on_startup():
    migrate_legacy_token()
    if AUTO_KILL_AFTER_SECONDS > 0:
        threading.Thread(target=_auto_kill_worker, daemon=True).start()
    try:
        report = load_expense_report("default")
        if report is None or not report.get("transactions"):
            recover_archived_transactions("default")
    except Exception:
        pass


def _write_oauth_state(state: str, profile_id: str) -> None:
    STATE_FILE.write_text(
        json.dumps({"state": state, "profile_id": profile_id}, indent=2),
        encoding="utf-8",
    )


def _read_oauth_state() -> tuple[str | None, str | None]:
    if STATE_FILE.exists():
        try:
            payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload.get("state"), payload.get("profile_id")
        except json.JSONDecodeError:
            legacy = STATE_FILE.read_text(encoding="utf-8").strip()
            return legacy or None, None
    return None, None


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(DASHBOARD_HTML)


@app.get("/api/profiles")
def api_profiles():
    return {"ok": True, "profiles": list_profiles(refresh_expired=True)}


@app.post("/api/expenses/scan")
def api_scan_expenses(
    profile_id: str = Query(default="default"),
    max_results: int = Query(default=100, ge=1, le=100),
):
    try:
        report = scan_expense_emails(profile_id=profile_id, max_results=max_results)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Expense scan failed: {exc}") from exc
    return {"ok": True, **report}


@app.get("/api/expenses/{profile_id}")
def api_get_expenses(profile_id: str):
    try:
        report = load_expense_report(profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not report:
        return {
            "ok": True,
            "profile_id": profile_id,
            "transactions": [],
            "summary": None,
            "total_transactions": 0,
        }
    report.setdefault("total_transactions", len(report.get("transactions", [])))
    return {"ok": True, **report}


@app.post("/api/expenses/recover")
def api_recover_expenses(profile_id: str = Query(default="default")):
    try:
        report = recover_archived_transactions(profile_id=profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Expense recovery failed: {exc}") from exc
    return {"ok": True, **report}


@app.get("/login")
def login(profile_id: str = Query(default="", description="Profile id for this account")):
    global _oauth_state, _oauth_profile_id

    if not CLIENT_ID or not CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Missing Google OAuth env vars")

    try:
        resolved_profile_id = sanitize_profile_id(profile_id) if profile_id.strip() else ""
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    login_url, state = build_google_login_url(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scopes=SCOPES,
    )
    _oauth_state = state
    _oauth_profile_id = resolved_profile_id
    _write_oauth_state(state, resolved_profile_id)
    return {"login_url": login_url, "profile_id": resolved_profile_id}


@app.get("/auth/callback")
def auth_callback(request: Request, background_tasks: BackgroundTasks):
    state = _oauth_state
    profile_id = _oauth_profile_id
    if not state or profile_id is None:
        stored_state, stored_profile_id = _read_oauth_state()
        state = state or stored_state
        if profile_id is None:
            profile_id = stored_profile_id
    if not state:
        raise HTTPException(status_code=400, detail="OAuth state not found. Call /login first.")
    if request.query_params.get("error"):
        raise HTTPException(
            status_code=400,
            detail=f"Google OAuth error: {request.query_params.get('error')}",
        )

    try:
        token_data = verify_google_oauth_callback(
            callback_url=str(request.url),
            expected_state=state,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri=REDIRECT_URI,
            scopes=SCOPES,
        )
        pid, email, name = resolve_profile_id_for_oauth(profile_id, token_data)
        saved = save_profile(pid, token_data, email=email, name=name)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"OAuth callback failed: {exc}") from exc

    if STATE_FILE.exists():
        STATE_FILE.unlink()

    if AUTO_KILL_AFTER_SECONDS > 0:
        background_tasks.add_task(_stop_self)

    return {
        "message": "Profile connected",
        "profile_id": saved["profile_id"],
        "email": saved["email"],
        "name": saved["name"],
    }


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Hermes</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #0f172a;
      --panel: #111827;
      --text: #e5e7eb;
      --muted: #9ca3af;
      --accent: #38bdf8;
      --ok: #34d399;
      --warn: #fbbf24;
      --border: #1f2937;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif;
      background: linear-gradient(180deg, #020617 0%, var(--bg) 100%);
      color: var(--text);
      min-height: 100vh;
    }
    main { max-width: 960px; margin: 0 auto; padding: 32px 20px 48px; }
    h1 { margin: 0 0 8px; font-size: 1.75rem; }
    p.lead { color: var(--muted); margin: 0 0 24px; }
    .toolbar {
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      align-items: end;
      margin-bottom: 24px;
      padding: 16px;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: rgba(17, 24, 39, 0.8);
    }
    label { display: block; font-size: 0.85rem; color: var(--muted); margin-bottom: 6px; }
    input {
      background: #0b1220;
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 8px;
      padding: 10px 12px;
      min-width: 220px;
    }
    button, a.button {
      appearance: none;
      border: none;
      border-radius: 8px;
      padding: 10px 14px;
      font-weight: 600;
      cursor: pointer;
      text-decoration: none;
      display: inline-block;
    }
    button.primary, a.button.primary { background: var(--accent); color: #082f49; }
    button.secondary { background: #1f2937; color: var(--text); }
    .grid { display: grid; gap: 12px; }
    .card {
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
      background: rgba(17, 24, 39, 0.75);
    }
    .card h2 { margin: 0 0 4px; font-size: 1.05rem; }
    .meta { color: var(--muted); font-size: 0.9rem; }
    .badge {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 0.75rem;
      font-weight: 700;
      margin-left: 8px;
    }
    .badge.ok { background: rgba(52, 211, 153, 0.15); color: var(--ok); }
    .badge.warn { background: rgba(251, 191, 36, 0.15); color: var(--warn); }
    .empty { color: var(--muted); padding: 24px; text-align: center; border: 1px dashed var(--border); border-radius: 12px; }
    .status { margin-top: 12px; font-size: 0.85rem; color: var(--muted); }
    .tabs {
      display: flex;
      gap: 8px;
      margin-bottom: 24px;
      border-bottom: 1px solid var(--border);
      padding-bottom: 0;
    }
    .tab {
      background: transparent;
      color: var(--muted);
      border: none;
      border-bottom: 2px solid transparent;
      border-radius: 0;
      padding: 10px 16px;
      font-weight: 600;
    }
    .tab.active {
      color: var(--accent);
      border-bottom-color: var(--accent);
    }
    .panel { display: none; }
    .panel.active { display: block; }
    .summary-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 12px;
      margin-bottom: 20px;
    }
    .stat {
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px;
      background: rgba(15, 23, 42, 0.6);
    }
    .stat .label { color: var(--muted); font-size: 0.8rem; margin-bottom: 6px; }
    .stat .value { font-size: 1.25rem; font-weight: 700; }
    .stat.debit .value { color: #f87171; }
    .stat.credit .value { color: var(--ok); }
    .stat.total .value { color: #c4b5fd; }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.88rem;
    }
    th, td {
      text-align: left;
      padding: 10px 12px;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
    }
    th { color: var(--muted); font-weight: 600; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.04em; }
    .type-debit { color: #f87171; font-weight: 700; }
    .type-credit { color: var(--ok); font-weight: 700; }
    .card-actions { margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap; }
    .table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 12px; }
    .hidden { display: none !important; }
    .sub-tabs {
      display: flex;
      gap: 10px;
      margin: 0 0 12px;
      padding: 12px;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: rgba(15, 23, 42, 0.7);
    }
    .sub-tab {
      background: #1f2937;
      color: var(--muted);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 18px;
      font-size: 0.95rem;
      font-weight: 700;
      cursor: pointer;
    }
    .sub-tab.active {
      background: rgba(56, 189, 248, 0.18);
      color: var(--accent);
      border-color: rgba(56, 189, 248, 0.45);
    }
    .filter-bar {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: end;
      margin-bottom: 16px;
      padding: 14px;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: rgba(17, 24, 39, 0.75);
    }
    .filter-presets {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      flex: 1;
    }
    .filter-preset {
      background: #1f2937;
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 7px 12px;
      font-size: 0.82rem;
      font-weight: 600;
      cursor: pointer;
    }
    .filter-preset.active {
      background: rgba(52, 211, 153, 0.15);
      color: var(--ok);
      border-color: rgba(52, 211, 153, 0.35);
    }
    .filter-custom {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: end;
    }
    .filter-custom label {
      display: block;
      font-size: 0.78rem;
      color: var(--muted);
      margin-bottom: 4px;
    }
    .filter-custom input[type="date"] {
      min-width: 150px;
      padding: 8px 10px;
    }
    .stat.invest .value { color: #c4b5fd; }
  </style>
</head>
<body>
  <main>
    <h1>Hermes</h1>
    <p class="lead">Google email profiles and spend analysis from transaction alerts.</p>

    <nav class="tabs">
      <button class="tab active" data-tab="profiles">Profiles</button>
      <button class="tab" data-tab="expenses">Expenses</button>
    </nav>

    <section id="profilesPanel" class="panel active">
      <div class="toolbar">
        <div>
          <label for="profileId">Profile id (optional — auto-assigned for additional accounts)</label>
          <input id="profileId" placeholder="Leave blank to add another account" />
        </div>
        <button class="primary" id="connectBtn">Connect Google account</button>
        <button class="secondary" id="refreshBtn">Refresh</button>
      </div>
      <div id="profiles" class="grid"></div>
    </section>

    <section id="expensesPanel" class="panel">
      <div id="expenseProfiles" class="grid"></div>
      <div id="expenseResults" class="hidden">
        <nav class="sub-tabs" id="expenseSubTabs">
          <button class="sub-tab active" data-view="spend">Spend</button>
          <button class="sub-tab" data-view="investments">Investments</button>
        </nav>
        <div class="filter-bar" id="expenseFilters">
          <div class="filter-presets">
            <button class="filter-preset active" data-preset="all">All time</button>
            <button class="filter-preset" data-preset="thisWeek">This week</button>
            <button class="filter-preset" data-preset="lastWeek">Last week</button>
            <button class="filter-preset" data-preset="thisMonth">This month</button>
            <button class="filter-preset" data-preset="lastMonth">Last month</button>
            <button class="filter-preset" data-preset="last30">Last 30 days</button>
          </div>
          <div class="filter-custom">
            <div>
              <label for="filterFrom">From</label>
              <input type="date" id="filterFrom" />
            </div>
            <div>
              <label for="filterTo">To</label>
              <input type="date" id="filterTo" />
            </div>
            <button class="secondary" id="applyDateFilter">Apply range</button>
          </div>
        </div>
        <div class="summary-grid" id="expenseSummary"></div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Type</th>
                <th>Amount</th>
                <th>Merchant / Description</th>
                <th>Source</th>
                <th>Category</th>
                <th>Date</th>
              </tr>
            </thead>
            <tbody id="expenseTableBody"></tbody>
          </table>
        </div>
      </div>
    </section>

    <div class="status" id="status"></div>
  </main>
  <script>
    const profilesEl = document.getElementById("profiles");
    const expenseProfilesEl = document.getElementById("expenseProfiles");
    const expenseResultsEl = document.getElementById("expenseResults");
    const expenseSummaryEl = document.getElementById("expenseSummary");
    const expenseTableBodyEl = document.getElementById("expenseTableBody");
    const statusEl = document.getElementById("status");
    const profileIdInput = document.getElementById("profileId");
    let cachedProfiles = [];

    function fmtMoney(n) {
      return new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }).format(n || 0);
    }

    function fmtDate(iso) {
      if (!iso) return "—";
      const dt = new Date(iso);
      if (Number.isNaN(dt.getTime())) return iso;
      return new Intl.DateTimeFormat("en-IN", {
        year: "numeric",
        month: "short",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: true,
        timeZone: "Asia/Kolkata",
      }).format(dt);
    }

    function sortTransactions(transactions) {
      return [...(transactions || [])].sort(
        (a, b) => new Date(b.transaction_at || 0) - new Date(a.transaction_at || 0)
      );
    }

    function badge(profile) {
      if (!profile.has_token) return '<span class="badge warn">No token</span>';
      if (profile.is_expired) return '<span class="badge warn">Expired</span>';
      return '<span class="badge ok">Active</span>';
    }

    function renderProfiles(profiles) {
      cachedProfiles = profiles;
      if (!profiles.length) {
        const empty = '<div class="empty">No profiles connected yet. Use Connect Google account above.</div>';
        profilesEl.innerHTML = empty;
        expenseProfilesEl.innerHTML = empty;
        return;
      }
      profilesEl.innerHTML = profiles.map((p) => `
        <article class="card">
          <h2>${p.name || p.email || p.profile_id}${badge(p)}</h2>
          <div class="meta">${p.email || "Unknown email"}</div>
          <div class="meta">Profile id: <code>${p.profile_id}</code></div>
          <div class="meta">Expiry: ${p.expiry || "unknown"}</div>
          <div class="meta">Connected: ${p.connected_at || "unknown"}</div>
        </article>
      `).join("");

      expenseProfilesEl.innerHTML = profiles.map((p) => `
        <article class="card">
          <h2>${p.name || p.email || p.profile_id}${badge(p)}</h2>
          <div class="meta">${p.email || "Unknown email"}</div>
          <div class="meta">Profile id: <code>${p.profile_id}</code></div>
          <div class="card-actions">
            <button class="primary track-expenses-btn" data-profile-id="${p.profile_id}">
              Sync expenses
            </button>
            <button class="secondary load-expenses-btn" data-profile-id="${p.profile_id}">
              View saved
            </button>
          </div>
        </article>
      `).join("");

      document.querySelectorAll(".track-expenses-btn").forEach((btn) => {
        btn.addEventListener("click", () => scanExpenses(btn.dataset.profileId));
      });
      document.querySelectorAll(".load-expenses-btn").forEach((btn) => {
        btn.addEventListener("click", () => loadSavedExpenses(btn.dataset.profileId));
      });
    }

    let activeExpenseProfile = null;
    let cachedExpenseReport = null;
    let activeExpenseView = "spend";
    let activeDatePreset = "all";

    function pad2(n) {
      return String(n).padStart(2, "0");
    }

    function istParts(date = new Date()) {
      const parts = new Intl.DateTimeFormat("en-CA", {
        timeZone: "Asia/Kolkata",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      }).formatToParts(date);
      const get = (type) => Number(parts.find((p) => p.type === type).value);
      return { y: get("year"), m: get("month"), d: get("day") };
    }

    function istBounds(y, m, d, endOfDay = false) {
      const suffix = endOfDay ? "T23:59:59.999+05:30" : "T00:00:00+05:30";
      return new Date(`${y}-${pad2(m)}-${pad2(d)}${suffix}`);
    }

    function lastDayOfMonth(y, m) {
      return new Date(Date.UTC(y, m, 0)).getUTCDate();
    }

    function getDateRange(preset) {
      const { y, m, d } = istParts();
      const todayStart = istBounds(y, m, d, false);
      const todayEnd = istBounds(y, m, d, true);

      if (preset === "all") return { from: null, to: null };

      if (preset === "last30") {
        return { from: new Date(todayStart.getTime() - 30 * 86400000), to: todayEnd };
      }

      if (preset === "thisMonth") {
        return { from: istBounds(y, m, 1, false), to: todayEnd };
      }

      if (preset === "lastMonth") {
        const lm = m === 1 ? 12 : m - 1;
        const ly = m === 1 ? y - 1 : y;
        const last = lastDayOfMonth(ly, lm);
        return { from: istBounds(ly, lm, 1, false), to: istBounds(ly, lm, last, true) };
      }

      const weekday = new Date(`${y}-${pad2(m)}-${pad2(d)}T12:00:00+05:30`).getUTCDay();
      const mondayOffset = weekday === 0 ? 6 : weekday - 1;
      const thisWeekStart = new Date(todayStart.getTime() - mondayOffset * 86400000);

      if (preset === "thisWeek") {
        return { from: thisWeekStart, to: todayEnd };
      }

      if (preset === "lastWeek") {
        const lastWeekEnd = new Date(thisWeekStart.getTime() - 1);
        const lastWeekStart = new Date(thisWeekStart.getTime() - 7 * 86400000);
        return { from: lastWeekStart, to: lastWeekEnd };
      }

      return { from: null, to: null };
    }

    function getCustomDateRange() {
      const fromInput = document.getElementById("filterFrom").value;
      const toInput = document.getElementById("filterTo").value;
      let from = null;
      let to = null;
      if (fromInput) {
        const [fy, fm, fd] = fromInput.split("-").map(Number);
        from = istBounds(fy, fm, fd, false);
      }
      if (toInput) {
        const [ty, tm, td] = toInput.split("-").map(Number);
        to = istBounds(ty, tm, td, true);
      }
      return { from, to };
    }

    function resolveActiveDateRange() {
      if (activeDatePreset === "custom") return getCustomDateRange();
      return getDateRange(activeDatePreset);
    }

    function parseTxnDate(t) {
      if (!t.transaction_at) return null;
      const dt = new Date(t.transaction_at);
      return Number.isNaN(dt.getTime()) ? null : dt;
    }

    function filterByDateRange(transactions, range) {
      if (!range.from && !range.to) return transactions;
      return transactions.filter((t) => {
        const dt = parseTxnDate(t);
        if (!dt) return false;
        if (range.from && dt < range.from) return false;
        if (range.to && dt > range.to) return false;
        return true;
      });
    }

    function presetLabel(preset) {
      return ({
        all: "All time",
        thisWeek: "This week",
        lastWeek: "Last week",
        thisMonth: "This month",
        lastMonth: "Last month",
        last30: "Last 30 days",
        custom: "Custom range",
      })[preset] || "Filtered";
    }

    function isInvestment(t) {
      return t.category === "Investment";
    }

    function filterByView(transactions, view) {
      return (transactions || []).filter((t) =>
        view === "investments" ? isInvestment(t) : !isInvestment(t)
      );
    }

    function buildSectionSummary(transactions) {
      const debits = transactions.filter((t) => t.type === "debit");
      const credits = transactions.filter((t) => t.type === "credit");
      const totalDebit = debits.reduce((sum, t) => sum + (t.amount || 0), 0);
      const totalCredit = credits.reduce((sum, t) => sum + (t.amount || 0), 0);
      return {
        total_debit: totalDebit,
        total_credit: totalCredit,
        net: totalDebit - totalCredit,
        debit_count: debits.length,
        credit_count: credits.length,
        transaction_count: transactions.length,
      };
    }

    function renderExpenseView(view) {
      activeExpenseView = view;
      document.querySelectorAll("#expenseSubTabs .sub-tab").forEach((tab) => {
        tab.classList.toggle("active", tab.dataset.view === view);
      });
      if (!cachedExpenseReport) return;

      const all = cachedExpenseReport.transactions || [];
      const byView = filterByView(all, view);
      const range = resolveActiveDateRange();
      const filtered = filterByDateRange(byView, range);
      const summary = buildSectionSummary(filtered);
      const lastScan = cachedExpenseReport.last_scan || {};
      const newCount = cachedExpenseReport.transactions_found ?? lastScan.new_transactions ?? 0;
      const netLabel = view === "investments" ? "Net invested" : "Net spend";
      const netClass = view === "investments" ? "invest" : "net";
      const periodLabel = activeDatePreset === "all" ? "All time" : presetLabel(activeDatePreset);

      expenseSummaryEl.innerHTML = `
        <div class="stat total"><div class="label">${view === "investments" ? "Investment" : "Spend"} transactions</div><div class="value">${filtered.length}</div></div>
        <div class="stat debit"><div class="label">Total debits (${summary.debit_count || 0})</div><div class="value">${fmtMoney(summary.total_debit)}</div></div>
        <div class="stat credit"><div class="label">Total credits (${summary.credit_count || 0})</div><div class="value">${fmtMoney(summary.total_credit)}</div></div>
        <div class="stat ${netClass}"><div class="label">${netLabel}</div><div class="value">${fmtMoney(summary.net)}</div></div>
        <div class="stat"><div class="label">Period</div><div class="value" style="font-size:1rem">${periodLabel}</div></div>
        ${view === "spend" && activeDatePreset === "all" ? `<div class="stat"><div class="label">New this sync</div><div class="value">${newCount}</div></div>` : ""}
      `;

      if (!filtered.length) {
        expenseTableBodyEl.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--muted)">No ${view === "investments" ? "investment" : "spend"} transactions for ${periodLabel.toLowerCase()}.</td></tr>`;
        return;
      }

      expenseTableBodyEl.innerHTML = sortTransactions(filtered).map((t) => `
        <tr>
          <td class="type-${t.type}">${t.type.toUpperCase()}</td>
          <td>${fmtMoney(t.amount)}</td>
          <td>${t.merchant || t.subject || "—"}</td>
          <td>${t.source || "—"}</td>
          <td>${t.category || "—"}</td>
          <td title="${t.transaction_at || ""}">${fmtDate(t.transaction_at || t.date)}</td>
        </tr>
      `).join("");
    }

    function renderExpenseReport(report, statusMessage) {
      cachedExpenseReport = report;
      const total = report?.total_transactions ?? (report?.transactions?.length || 0);
      const lastScan = report?.last_scan || {};

      if (!report || !report.transactions || !report.transactions.length) {
        expenseResultsEl.classList.remove("hidden");
        expenseSummaryEl.innerHTML = `
          <div class="stat"><div class="label">Stored transactions</div><div class="value">0</div></div>
          <div class="stat"><div class="label">Last sync scanned</div><div class="value">${report?.emails_scanned || lastScan.emails_scanned || 0}</div></div>
        `;
        expenseTableBodyEl.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--muted)">No transactions stored yet. Sync scans new inbox emails and merges them here.</td></tr>';
        if (statusMessage) statusEl.textContent = statusMessage;
        return;
      }

      renderExpenseView(activeExpenseView);
      expenseResultsEl.classList.remove("hidden");
      if (statusMessage) statusEl.textContent = statusMessage;
    }

    async function loadSavedExpenses(profileId, quiet = false) {
      activeExpenseProfile = profileId;
      if (!quiet) statusEl.textContent = `Loading saved transactions for ${profileId}...`;
      try {
        const res = await fetch(`/api/expenses/${encodeURIComponent(profileId)}`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Load failed");
        const total = data.total_transactions ?? (data.transactions || []).length;
        renderExpenseReport(
          data,
          quiet ? null : `${total} stored transaction(s) loaded from local database.`
        );
      } catch (err) {
        statusEl.textContent = String(err.message || err);
      }
    }

    async function scanExpenses(profileId) {
      activeExpenseProfile = profileId;
      statusEl.textContent = `Syncing up to 100 inbox emails for ${profileId}...`;
      try {
        const res = await fetch(`/api/expenses/scan?profile_id=${encodeURIComponent(profileId)}&max_results=100`, {
          method: "POST",
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Sync failed");
        const total = data.total_transactions ?? (data.transactions || []).length;
        const newCount = data.transactions_found ?? data.last_scan?.new_transactions ?? 0;
        renderExpenseReport(
          data,
          `Sync complete: ${newCount} new, ${total} total stored. Archived ${data.emails_archived || 0}.`
        );
      } catch (err) {
        statusEl.textContent = String(err.message || err);
      }
    }

    async function loadProfiles() {
      statusEl.textContent = "Loading profiles...";
      try {
        const res = await fetch("/api/profiles");
        const data = await res.json();
        renderProfiles(data.profiles || []);
        statusEl.textContent = `${(data.profiles || []).length} profile(s) loaded.`;
      } catch (err) {
        statusEl.textContent = "Failed to load profiles.";
        profilesEl.innerHTML = '<div class="empty">Could not reach /api/profiles.</div>';
      }
    }

    async function connectAccount() {
      const profileId = profileIdInput.value.trim();
      statusEl.textContent = "Starting OAuth...";
      try {
        const loginPath = profileId
          ? `/login?profile_id=${encodeURIComponent(profileId)}`
          : "/login";
        const res = await fetch(loginPath);
        const data = await res.json();
        if (!res.ok || !data.login_url) {
          throw new Error(data.detail || "Failed to start OAuth");
        }
        window.location.href = data.login_url;
      } catch (err) {
        statusEl.textContent = String(err.message || err);
      }
    }

    document.querySelectorAll(".tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
        document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
        tab.classList.add("active");
        document.getElementById(`${tab.dataset.tab}Panel`).classList.add("active");
        if (tab.dataset.tab === "expenses") {
          const preferred = cachedProfiles.find((p) => p.profile_id === "default") || cachedProfiles[0];
          if (preferred) loadSavedExpenses(preferred.profile_id, true);
        }
      });
    });

    document.getElementById("expenseSubTabs").addEventListener("click", (event) => {
      const tab = event.target.closest(".sub-tab");
      if (!tab) return;
      renderExpenseView(tab.dataset.view);
    });

    document.getElementById("expenseFilters").addEventListener("click", (event) => {
      const btn = event.target.closest(".filter-preset");
      if (!btn) return;
      activeDatePreset = btn.dataset.preset;
      document.querySelectorAll(".filter-preset").forEach((el) => {
        el.classList.toggle("active", el.dataset.preset === activeDatePreset);
      });
      if (activeDatePreset !== "custom") {
        document.getElementById("filterFrom").value = "";
        document.getElementById("filterTo").value = "";
      }
      renderExpenseView(activeExpenseView);
    });

    document.getElementById("applyDateFilter").addEventListener("click", () => {
      activeDatePreset = "custom";
      document.querySelectorAll(".filter-preset").forEach((el) => {
        el.classList.remove("active");
      });
      renderExpenseView(activeExpenseView);
    });

    document.getElementById("refreshBtn").addEventListener("click", loadProfiles);
    document.getElementById("connectBtn").addEventListener("click", connectAccount);
    loadProfiles();
  </script>
</body>
</html>
"""
