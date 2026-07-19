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

Property names expected on the trades data source (edit PROP_* below if yours differ):
  Date            (date)
  REALIZED PNL    (number)
  OUTCOME         (select: WIN / LOSS / BREAKEVEN)  -- note trailing space in Notion
  AM FRAMEWORK    (relation -> AM FRAMEWORK db)      -- emoji prefix in Notion: "🟦 AM FRAMEWORK"
  PM FRAMEWORK    (relation -> PM FRAMEWORK db)      -- emoji prefix in Notion: "🟧 PM FRAMEWORK"
"""

import os
import sys
import json
from collections import defaultdict
from datetime import datetime, timezone
import urllib.request
import urllib.error

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
TRADES_DS = os.environ["NOTION_TRADES_DATA_SOURCE_ID"]
AM_DS = os.environ["NOTION_AM_FRAMEWORK_DATA_SOURCE_ID"]
PM_DS = os.environ["NOTION_PM_FRAMEWORK_DATA_SOURCE_ID"]

NOTION_VERSION = "2025-09-03"
API_BASE = "https://api.notion.com/v1"

# --- property names on the trades data source. Edit if yours are named differently. ---
PROP_DATE = "Date"
PROP_PNL = "REALIZED PNL"
PROP_OUTCOME = "OUTCOME "          # trailing space intentional
PROP_AM_REL = "🟦 AM FRAMEWORK"
PROP_PM_REL = "🟧 PM FRAMEWORK"


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
            return "".join(p.get("plain_text", "") for p in parts) or "Untitled"
    return "Untitled"


def build_name_map(data_source_id):
    pages = query_all(data_source_id)
    return {p["id"]: get_title(p) for p in pages}


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

        trades.append({
            "date": date_val[:10],
            "pnl": pnl_val,
            "outcome": outcome,
            "frameworks": [am_names.get(i, "Unknown AM") for i in am_ids]
                        + [pm_names.get(i, "Unknown PM") for i in pm_ids],
        })
    trades.sort(key=lambda t: t["date"])
    return trades


def compute_stats(trades):
    stats = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "be": 0,
                                  "net": 0.0, "gross_win": 0.0, "gross_loss": 0.0})
    series = defaultdict(list)
    running = defaultdict(float)

    for t in trades:
        fw_list = t["frameworks"] or ["Untagged"]
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
            round(s["gross_win"], 2) if s["gross_win"] > 0 else 0)
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
    am_names = build_name_map(AM_DS)
    pm_names = build_name_map(PM_DS)
    trades = extract_trades(am_names, pm_names)
    frameworks = compute_stats(trades)

    overall_running = 0.0
    overall_series = []
    for t in trades:
        overall_running += t["pnl"]
        overall_series.append({"date": t["date"], "cum": round(overall_running, 2)})

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_trades": len(trades),
        "total_pnl": round(sum(t["pnl"] for t in trades), 2),
        "overall_series": overall_series,
        "frameworks": frameworks,
    }

    out_path = os.path.join(os.path.dirname(__file__), "data.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)

    print(f"Synced {len(trades)} trades across {len(frameworks)} frameworks -> {out_path}")


if __name__ == "__main__":
    main()
