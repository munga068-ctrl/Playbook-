"""
sync_notion.py
----------------
Pulls trades from the MMTrades DASHBOARD data source in Notion, joins them
against the AM FRAMEWORK / PM FRAMEWORK relation databases, computes
per-framework performance stats (win rate, net P&L, profit factor,
expectancy, avg win/loss) plus a cumulative-P&L time series per framework,
and writes the result to data.json for the playbook dashboard to consume.

Environment variables required (set these as GitHub Actions secrets):
  NOTION_TOKEN                        - Notion internal integration token
  NOTION_TRADES_DATA_SOURCE_ID        - data source id of the DASHBOARD/trades table
  NOTION_AM_FRAMEWORK_DATA_SOURCE_ID  - data source id of AM FRAMEWORK
  NOTION_PM_FRAMEWORK_DATA_SOURCE_ID  - data source id of PM FRAMEWORK

Optional (enables the AM Framework heatmap -> am-frameworks.json):
  NOTION_INDICES_LOG_DATA_SOURCE_ID   - data source id of the INDICES LOG (one row per
                                        logged trading day, with an AM FRAMEWORK relation)

All ID secrets are cleaned on read (whitespace and stray quote characters are
stripped) and must be a UUID, with or without dashes. Use the DATA SOURCE id,
not the database id.

Property names expected on the trades data source (edit PROP_* below if yours differ):
  Date            (date)
  REALIZED PNL    (number)
  OUTCOME         (select: WIN / LOSS / BREAKEVEN)  -- note trailing space in Notion
  AM FRAMEWORK    (relation -> AM FRAMEWORK db)      -- emoji prefix in Notion: "🟦 AM FRAMEWORK"
  PM FRAMEWORK    (relation -> PM FRAMEWORK db)      -- emoji prefix in Notion: "🟧 PM FRAMEWORK"
"""

import os
import re
import sys
import json
from collections import defaultdict
from datetime import datetime, timezone
import urllib.request
import urllib.error

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$"
)


def _env_id(name, required=True):
    """Read a Notion ID from the environment, tolerating stray whitespace and quote
    characters (e.g. a secret saved as "abc..." with the quotes included).

    Required IDs raise a clear error if missing/invalid. Optional IDs return ""
    (and print a warning if set but invalid) so the main trades sync still runs."""
    val = os.environ.get(name, "").strip().strip("\"'").strip()
    if not val:
        if required:
            raise KeyError(name)
        return ""
    if not _UUID_RE.match(val):
        msg = f"{name} is not a valid Notion ID after cleaning (expected a UUID)"
        if required:
            raise ValueError(msg)
        print(f"::warning::{msg} - skipping", file=sys.stderr)
        return ""
    return val


NOTION_TOKEN = os.environ["NOTION_TOKEN"]
TRADES_DS = _env_id("NOTION_TRADES_DATA_SOURCE_ID")
AM_DS = _env_id("NOTION_AM_FRAMEWORK_DATA_SOURCE_ID")
PM_DS = _env_id("NOTION_PM_FRAMEWORK_DATA_SOURCE_ID")
LOG_DS = _env_id("NOTION_INDICES_LOG_DATA_SOURCE_ID", required=False)

NOTION_VERSION = "2025-09-03"
API_BASE = "https://api.notion.com/v1"

# --- property names on the trades data source. Edit if yours are named differently. ---
PROP_DATE = "Date"
PROP_PNL = "REALIZED PNL"
PROP_OUTCOME = "OUTCOME "          # trailing space intentional
PROP_AM_REL = "🟦 AM FRAMEWORK"
PROP_PM_REL = "🟧 PM FRAMEWORK"

# --- property names on the INDICES LOG data source (heatmap). If these don't match
# exactly, the script falls back to the first date property / the first relation
# property whose name contains "AM FRAMEWORK", so a renamed column won't break it. ---
PROP_LOG_DATE = "Date"
PROP_LOG_AM_REL = "🟦 AM FRAMEWORK"

# --- event tags looked for in the Indices Log row Name (title). A day carrying any of
# these is a "low-probability day" on the dashboard (FOMC/CPI/NFP also flag the day before;
# that part is worked out in index.html). Matched as whole words, case-insensitive. ---
LOW_PROB_TAGS = ("FOMC", "CPI", "NFP", "PPI")


def notion_request(path, payload=None):
    url = f"{API_BASE}{path}"
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {NOTION_TOKEN}")
    req.add_header("Notion-Version", NOTION_VERSION)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"Notion API error {e.code} on {path}: {e.read().decode('utf-8')}", file=sys.stderr)
        raise


def query_all(data_source_id):
    results = []
    cursor = None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        resp = notion_request(f"/data_sources/{data_source_id}/query", payload)
        results.extend(resp.get("results", []))
        if resp.get("has_more"):
            cursor = resp.get("next_cursor")
        else:
            break
    return results


def get_title(page):
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            parts = prop.get("title", [])
            text = "".join(p.get("plain_text", "") for p in parts).strip()
            return text or "Untitled"
    return "Untitled"


def build_name_map(data_source_id):
    pages = query_all(data_source_id)
    return {p["id"]: get_title(p) for p in pages}


def _find_prop(props, preferred, ptype, name_hint=None):
    """Return the property named `preferred` if it has the right type; otherwise the
    first property of `ptype` (whose name contains `name_hint`, when given)."""
    p = props.get(preferred)
    if p and p.get("type") == ptype:
        return p
    for name, prop in props.items():
        if prop.get("type") != ptype:
            continue
        if name_hint and name_hint.upper() not in name.upper():
            continue
        return prop
    return None


def build_heatmap(am_pages, log_ds):
    """AM Framework probability heatmap data.

    Every row of the Indices Log is a logged trading day; a day that has an AM
    FRAMEWORK related to it is a 'high-probability day'. Output matches the shape
    index.html reads: frameworks (colour order = creation order, so colours stay
    stable as new frameworks are added), logged dates, per-day framework indexes,
    and events (days whose Name carries an FOMC / CPI / NFP / PPI tag).
    """
    ordered = sorted(am_pages, key=lambda p: p.get("created_time", ""))
    frameworks, index = [], {}
    for p in ordered:
        name = get_title(p)
        entry = {"name": name}
        if name.strip().upper().startswith("NO FRAMEWORK"):
            entry["neutral"] = True
        index[p["id"]] = len(frameworks)
        frameworks.append(entry)

    logged, by_day, events = set(), defaultdict(set), []
    rel_seen = False
    for page in query_all(log_ds):
        props = page.get("properties", {})
        date_prop = _find_prop(props, PROP_LOG_DATE, "date")
        start = ((date_prop or {}).get("date") or {}).get("start")
        if not start:
            continue
        day = start[:10]
        logged.add(day)
        title = get_title(page)
        if any(re.search(r"\b" + tag + r"\b", title, re.I) for tag in LOW_PROB_TAGS):
            events.append([day, title])
        rel_prop = _find_prop(props, PROP_LOG_AM_REL, "relation", name_hint="AM FRAMEWORK")
        if rel_prop is not None:
            rel_seen = True
            for r in rel_prop.get("relation", []):
                if r["id"] in index:
                    by_day[day].add(index[r["id"]])

    if not logged:
        raise RuntimeError("Indices Log returned no dated rows (check the data source id / integration access)")
    if not rel_seen:
        raise RuntimeError("no AM FRAMEWORK relation property found on the Indices Log (check PROP_LOG_AM_REL)")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "frameworks": frameworks,
        "logged": sorted(logged),
        "days": [[d, sorted(ix)] for d, ix in sorted(by_day.items()) if ix],
        "events": sorted(events),
    }


def sync_heatmap(am_pages):
    if not LOG_DS:
        print("::notice::NOTION_INDICES_LOG_DATA_SOURCE_ID is not set (or invalid) - skipping heatmap sync")
        return
    out_path = os.path.join(os.path.dirname(__file__), "am-frameworks.json")
    try:
        heat = build_heatmap(am_pages, LOG_DS)
    except Exception as e:  # never let the heatmap take down the main trades sync
        print(f"::warning::Heatmap sync failed, keeping the previous am-frameworks.json: {e}", file=sys.stderr)
        return
    # only rewrite when the content changed, so quiet days don't create empty commits
    try:
        with open(out_path) as f:
            prev = json.load(f)
        if {k: v for k, v in prev.items() if k != "generated_at"} == {k: v for k, v in heat.items() if k != "generated_at"}:
            print("Heatmap unchanged -> am-frameworks.json left as is")
            return
    except (OSError, ValueError):
        pass
    with open(out_path, "w") as f:
        json.dump(heat, f, separators=(",", ":"))
    print(f"Heatmap: {len(heat['logged'])} logged days, {len(heat['days'])} with an AM framework, "
          f"{len(heat['events'])} event-tagged days -> {out_path}")


def _normalize_framework_name(name):
    """Treat explicit 'NO FRAMEWORK AM' / 'NO FRAMEWORK PM' placeholder pages
    the same as a trade with no relation at all — both mean 'not really tied
    to a playbook' and should show up as a single '❌ No Setup' bucket rather
    than as their own framework cards."""
    if name.strip().upper().startswith("NO FRAMEWORK"):
        return "❌ No Setup"
    return name


def extract_trades(am_names, pm_names):
    pages = query_all(TRADES_DS)
    trades = []
    for p in pages:
        props = p.get("properties", {})

        date_val = None
        date_prop = props.get(PROP_DATE, {}).get("date")
        if date_prop:
            date_val = date_prop.get("start")

        pnl_val = props.get(PROP_PNL, {}).get("number")

        outcome_sel = props.get(PROP_OUTCOME, {}).get("select")
        outcome = outcome_sel.get("name") if outcome_sel else None

        am_ids = [r["id"] for r in props.get(PROP_AM_REL, {}).get("relation", [])]
        pm_ids = [r["id"] for r in props.get(PROP_PM_REL, {}).get("relation", [])]

        if date_val is None or pnl_val is None:
            continue  # skip incomplete rows

        raw_frameworks = [am_names.get(i, "Unknown AM") for i in am_ids] \
                        + [pm_names.get(i, "Unknown PM") for i in pm_ids]
        # normalize placeholder names, then dedupe so a trade tagged with
        # e.g. both "NO FRAMEWORK AM" and "NO FRAMEWORK PM" (or any other
        # repeated name) only counts once toward that bucket
        frameworks = list(dict.fromkeys(_normalize_framework_name(n) for n in raw_frameworks))

        trades.append({
            "date": date_val[:10],
            "pnl": pnl_val,
            "outcome": outcome,
            "frameworks": frameworks,
        })
    trades.sort(key=lambda t: t["date"])
    return trades


def compute_stats(trades):
    stats = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "be": 0,
                                  "net": 0.0, "gross_win": 0.0, "gross_loss": 0.0})
    series = defaultdict(list)
    running = defaultdict(float)

    for t in trades:
        fw_list = t["frameworks"] or ["❌ No Setup"]
        for fw in fw_list:
            s = stats[fw]
            s["trades"] += 1
            s["net"] += t["pnl"]
            if t["outcome"] == "WIN":
                s["wins"] += 1
                s["gross_win"] += t["pnl"]
            elif t["outcome"] == "LOSS":
                s["losses"] += 1
                s["gross_loss"] += abs(t["pnl"])
            else:
                s["be"] += 1
            running[fw] += t["pnl"]
            series[fw].append({"date": t["date"], "cum": round(running[fw], 2)})

    result = []
    for name, s in stats.items():
        n = s["trades"]
        win_rate = round(100 * s["wins"] / n, 2) if n else 0
        avg_win = round(s["gross_win"] / s["wins"], 2) if s["wins"] else 0
        avg_loss = round(s["gross_loss"] / s["losses"], 2) if s["losses"] else 0
        pf = round(s["gross_win"] / s["gross_loss"], 2) if s["gross_loss"] > 0 else (
            None if s["gross_win"] > 0 else 0)
        expectancy = round(s["net"] / n, 2) if n else 0
        result.append({
            "name": name, "trades": n, "wins": s["wins"], "losses": s["losses"], "be": s["be"],
            "win_rate": win_rate, "net_pnl": round(s["net"], 2),
            "avg_win": avg_win, "avg_loss": avg_loss,
            "profit_factor": pf, "expectancy": expectancy,
            "series": series[name],
        })
    result.sort(key=lambda x: -x["net_pnl"])
    return result


def main():
    am_pages = query_all(AM_DS)
    am_names = {p["id"]: get_title(p) for p in am_pages}
    pm_names = build_name_map(PM_DS)
    trades = extract_trades(am_names, pm_names)
    frameworks = compute_stats(trades)

    overall_running = 0.0
    overall_series = []
    for t in trades:
        overall_running += t["pnl"]
        overall_series.append({"date": t["date"], "cum": round(overall_running, 2)})

    # Trades tagged to more than one framework (e.g. both an AM and a PM
    # framework) are intentionally given full P&L credit in each one -- both
    # setups were valid for that trade, so neither should be shortchanged.
    # That means per-framework net_pnl figures will sum to more than
    # total_pnl below; this is by design, not a bug. total_pnl itself is
    # always derived straight from the raw trade list above, never from the
    # per-framework sums, so it stays accurate regardless. These two fields
    # just let the dashboard explain the difference to the reader.
    multi_tagged = sum(1 for t in trades if len(t["frameworks"] or []) > 1)
    attributed_pnl_total = round(sum(f["net_pnl"] for f in frameworks), 2)

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_trades": len(trades),
        "total_pnl": round(sum(t["pnl"] for t in trades), 2),
        "multi_tagged_trades": multi_tagged,
        "attributed_pnl_total": attributed_pnl_total,
        "overall_series": overall_series,
        "frameworks": frameworks,
    }

    out_path = os.path.join(os.path.dirname(__file__), "data.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)

    print(f"Synced {len(trades)} trades across {len(frameworks)} frameworks -> {out_path}")

    sync_heatmap(am_pages)


if __name__ == "__main__":
    main()
