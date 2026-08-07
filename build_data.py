#!/usr/bin/env python3
"""
build_data.py — Generate sanitized data.json for the OgleTrends dashboard.

Reads:
  1. IG queue state   (ig_queue_state.json — published/failed video numbers)
  2. IG account info  (via composio CLI — followers, media count)
  3. Gumroad sales    (API — revenue, units, AOV, product mix; NO emails)

Writes:
  data.json  (safe for public GitHub Pages — no tokens, no buyer PII)

Run every ~5-10 min via cron. On failure of a source, keeps last good data
and marks the source as stale rather than wiping the dashboard.
"""
import json
import os
import re
import subprocess
import sys
import datetime
import urllib.request

BASE = "/Users/mohindpa/Documents/My Files/Curiosity Projects/INSSIST"
STATE_FILE = os.path.join(BASE, "ig_queue_state.json")
OUT_FILE = os.path.join(BASE, "ogletrends-dashboard", "data.json")
COMPOSIO = os.path.expanduser("~/.composio")
ENV_FILE = os.path.expanduser("~/.hermes/.env")

TOTAL_VIDEOS = 57


def load_env():
    env = {}
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
    return env


def load_ig_state():
    """Return {published:[...], failed:[...], last_updated} from queue state."""
    if not os.path.exists(STATE_FILE):
        return {"published": [], "failed": {}, "source": "missing"}
    with open(STATE_FILE) as f:
        st = json.load(f)
    published = sorted(int(k) for k in st.get("published", {}) if str(k).isdigit())
    pub_set = set(published)
    # Only failures that are NOT also published (historical retry-success)
    failed = sorted(int(k) for k in st.get("failed", {}) if str(k).isdigit() and int(k) not in pub_set)
    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(STATE_FILE))
    return {
        "published": published,
        "failed": failed,
        "last_updated": mtime.isoformat(timespec="seconds"),
        "source": "local",
    }


def ig_account_info():
    """Followers + media count via composio CLI; None on failure (keep stale)."""
    try:
        cmd = ["composio", "execute", "INSTAGRAM_GET_USER_INFO", "--skip-tool-params-check", "-d", "{}"]
        env = dict(os.environ)
        env["PATH"] = f"{COMPOSIO}:{env.get('PATH', '')}"
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
        d = json.loads(out.stdout or out.stderr)
        if d.get("successful"):
            data = d["data"]
            return {
                "followers": data.get("followers_count"),
                "media_count": data.get("media_count"),
                "username": data.get("username"),
            }
    except Exception as e:
        print("ig_account_info error:", e, file=sys.stderr)
    return None


def gumroad_sales(access_token, days=14):
    """Fetch FULL sales history, return sanitized aggregates. NO emails or buyer PII."""
    sales = []
    page_key = None
    pages = 0
    while pages < 500:
        url = "https://api.gumroad.com/v2/sales?access_token=" + access_token
        if page_key:
            url += "&page_key=" + page_key
        with urllib.request.urlopen(url, timeout=30) as resp:
            d = json.loads(resp.read())
        if not d.get("success"):
            break
        batch = d.get("sales", [])
        if not batch:
            break
        sales.extend(batch)
        pages += 1
        nk = d.get("next_page_key")
        if not nk:
            break
        page_key = nk

    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=days)

    # GST day window (today = since 20:00 UTC yesterday)
    gst_now = now + datetime.timedelta(hours=4)
    today_start_utc = (gst_now.replace(hour=0, minute=0, second=0, microsecond=0)
                       - datetime.timedelta(hours=4))

    by_day = {}
    by_day_all = {}
    today_rev = 0.0
    today_units = 0
    product_rev = {}
    product_rev_all = {}
    total_rev = 0.0
    total_units = 0
    refunds = 0
    disputes = 0
    refund_amt = 0.0

    for s in sales:
        ts = s.get("created_at") or ""
        try:
            dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        price = float(s.get("price") or 0) / 100.0  # cents -> dollars
        paid = s.get("paid")
        if not paid:
            continue
        day = dt.date().isoformat()
        pname = (s.get("product_name") or "Unknown").split(" - ")[0]
        by_day_all[day] = by_day_all.get(day, 0.0) + price
        product_rev_all[pname] = product_rev_all.get(pname, 0.0) + price
        if s.get("refunded"):
            refunds += 1
            refund_amt += price
        if s.get("disputed"):
            disputes += 1
        if dt < cutoff:
            continue
        by_day[day] = by_day.get(day, 0.0) + price
        total_rev += price
        total_units += 1
        product_rev[pname] = product_rev.get(pname, 0.0) + price
        if dt >= today_start_utc:
            today_rev += price
            today_units += 1

    # Week-over-week from full daily series (last 7 vs prev 7)
    all_days = sorted(by_day_all.keys())
    def window_sum(days_list):
        return sum(by_day_all.get(d, 0.0) for d in days_list)
    last7 = all_days[-7:] if len(all_days) >= 7 else all_days
    prev7 = all_days[-14:-7] if len(all_days) >= 14 else []
    wow = {
        "current": round(window_sum(last7), 2),
        "previous": round(window_sum(prev7), 2),
        "change_pct": round((window_sum(last7) - window_sum(prev7)) / max(1.0, window_sum(prev7)) * 100, 1) if prev7 else None,
    }

    return {
        "today_revenue": round(today_rev, 2),
        "today_units": today_units,
        "total_revenue_14d": round(total_rev, 2),
        "total_units_14d": total_units,
        "aov_14d": round(total_rev / total_units, 2) if total_units else 0,
        "daily": [{"day": k, "revenue": round(v, 2)} for k, v in sorted(by_day.items())],
        "daily_all": [{"day": k, "revenue": round(v, 2)} for k, v in sorted(by_day_all.items())],
        "products": [{"name": k, "revenue": round(v, 2)} for k, v in
                     sorted(product_rev.items(), key=lambda x: -x[1])],
        "products_all": [{"name": k, "revenue": round(v, 2)} for k, v in
                         sorted(product_rev_all.items(), key=lambda x: -x[1])],
        "all_time": {
            "revenue": round(sum(by_day_all.values()), 2),
            "units": len(sales),
            "refunds": refunds,
            "disputes": disputes,
            "refund_amount": round(refund_amt, 2),
            "from": all_days[0] if all_days else None,
            "to": all_days[-1] if all_days else None,
        },
        "week_over_week": wow,
    }


def main():
    env = load_env()
    token = env.get("GUMROAD_ACCESS_TOKEN", "")

    # Previous data.json for stale-keeping
    prev = {"sources": {}}
    if os.path.exists(OUT_FILE):
        try:
            with open(OUT_FILE) as f:
                prev = json.load(f)
        except Exception:
            prev = {"sources": {}}

    result = {
        "generated_at": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=4)))
                        .isoformat(timespec="seconds"),
        "total_videos": TOTAL_VIDEOS,
        "ig": {"state": load_ig_state(), "account": None, "stale": False},
        "gumroad": None,
        "schedule": [
            {"time": "12:00", "tz": "GST"},
            {"time": "16:30", "tz": "GST"},
        ],
        "sources": {},
    }

    # IG account info — best effort
    acc = ig_account_info()
    if acc:
        result["ig"]["account"] = acc
        result["sources"]["ig_account"] = "ok"
    else:
        prev_acc = (prev.get("ig") or {}).get("account")
        if prev_acc:
            result["ig"]["account"] = prev_acc
            result["sources"]["ig_account"] = "stale"
        else:
            result["sources"]["ig_account"] = "unavailable"

    # Gumroad — required; on failure keep stale
    try:
        g = gumroad_sales(token, days=14)
        result["gumroad"] = g
        result["sources"]["gumroad"] = "ok"
    except Exception as e:
        print("gumroad error:", e, file=sys.stderr)
        prev_g = prev.get("gumroad")
        if prev_g:
            result["gumroad"] = prev_g
            result["sources"]["gumroad"] = "stale"
        else:
            result["sources"]["gumroad"] = "unavailable"

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w") as f:
        json.dump(result, f, indent=2)
    print("wrote", OUT_FILE)
    print("published:", len(result["ig"]["state"]["published"]),
          "| gumroad source:", result["sources"].get("gumroad"))


if __name__ == "__main__":
    main()
