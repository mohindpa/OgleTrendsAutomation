#!/usr/bin/env python3
"""
build_data.py — Generate sanitized data.json for the OgleTrends dashboard (multi-account).

Reads:
  1. IG queue state per account   (IG/<account>/state.json)
  2. IG account info per account  (via composio CLI with --account selector)
  3. Gumroad sales                (API — revenue, units, AOV, product mix; NO emails)

Writes:
  data.json  (safe for public GitHub Pages — no tokens, no buyer PII)

Multi-account model: `ig.accounts` is a dict keyed by account name; each entry
has its own state + account info. `ig.total` summarizes across accounts.
Gumroad is shared (both accounts sell the same books) — shown once, clearly.

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

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IG_BASE = os.path.join(os.path.dirname(BASE), "IG")
ACCOUNTS_FILE = os.path.join(IG_BASE, "accounts.json")
OUT_FILE = os.path.join(BASE, "ogletrends-dashboard", "data.json")
COMPOSIO = os.path.expanduser("~/.composio")
ENV_FILE = next((p for p in [
    os.path.join(os.environ.get("HERMES_HOME", ""), ".env"),
    os.path.expanduser("~/.hermes/.env"),
    os.path.expanduser("~/.env"),
] if p and os.path.exists(p)), os.path.expanduser("~/.hermes/.env"))


def load_env():
    env = {}
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
    return env


def load_accounts():
    if os.path.exists(ACCOUNTS_FILE):
        with open(ACCOUNTS_FILE) as f:
            return json.load(f)
    return {}


def load_ig_state(account_dir):
    """Return {published:[...], failed:[...], last_updated} from queue state."""
    state_file = os.path.join(account_dir, "state.json")
    if not os.path.exists(state_file):
        return {"published": [], "failed": [], "source": "missing"}
    with open(state_file) as f:
        st = json.load(f)
    published = sorted(int(k) for k in st.get("published", {}) if str(k).isdigit())
    pub_set = set(published)
    failed = sorted(int(k) for k in st.get("failed", {}) if str(k).isdigit() and int(k) not in pub_set)
    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(state_file))
    posted_at = {int(k): v for k, v in st.get("posted_at", {}).items() if str(k).isdigit()}
    return {
        "published": published,
        "posted_at": posted_at,
        "failed": failed,
        "last_updated": mtime.isoformat(timespec="seconds"),
        "source": "local",
    }


def load_server_buffer(account_dir, videos_total):
    """Count videos actually on disk (server buffer) + refill progress.
    Rules (A9): buffer target 50, refill at 10, trim beyond 50."""
    vdir = os.path.join(account_dir, "videos")
    on_disk = set()
    size_mb = 0
    if os.path.isdir(vdir):
        for f in os.listdir(vdir):
            if f.endswith(".mp4"):
                try:
                    on_disk.add(int(f.split(".")[0]))
                    size_mb += os.path.getsize(os.path.join(vdir, f)) / 1e6
                except (ValueError, OSError):
                    continue
    target = 50
    refill_at = 10
    return {
        "on_disk": sorted(on_disk),
        "count": len(on_disk),
        "size_mb": round(size_mb, 1),
        "target": target,
        "refill_at": refill_at,
        "refill_progress": round(min(100, len(on_disk) / target * 100)),
        "needs_refill": len(on_disk) <= refill_at,
        "over_target": max(0, len(on_disk) - target),
    }


def ig_account_info(composio_account):
    """Followers + media count via composio CLI with explicit account selector."""
    try:
        cmd = ["composio", "execute", "INSTAGRAM_GET_USER_INFO", "--account",
               composio_account, "--skip-tool-params-check", "-d", "{}"]
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
        price = float(s.get("price") or 0) / 100.0
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
    accounts = load_accounts()

    prev = {"sources": {}, "ig": {}}
    if os.path.exists(OUT_FILE):
        try:
            with open(OUT_FILE) as f:
                prev = json.load(f)
        except Exception:
            prev = {"sources": {}, "ig": {}}

    result = {
        "generated_at": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=4)))
                        .isoformat(timespec="seconds"),
        "total_videos": sum(a.get("videos", 0) for a in accounts.values()),
        "ig": {"accounts": {}, "total": {"published": 0, "failed": 0}},
        "gumroad": None,
        "schedule": [{"time": "18:00", "tz": "GST"}, {"time": "00:00", "tz": "GST"}],
        "slots": accounts.get("ogletrends", {}).get("slots", []),  # placeholder, replaced per-account below
        "sources": {},
    }

    # Per-account IG state + account info
    for name, cfg in accounts.items():
        adir = os.path.join(IG_BASE, name)
        state = load_ig_state(adir)
        acc = ig_account_info(cfg.get("composio_account", ""))
        entry = {
            "name": cfg.get("display_name", name),
            "username": name,
            "composio_account": cfg.get("composio_account"),
            "videos": cfg.get("videos", 0),
            "state": state,
            "server_buffer": load_server_buffer(adir, cfg.get("videos", 0)),
            "account": acc,
            "slots": cfg.get("slots", []),
            "stale": acc is None,
        }
        result["ig"]["accounts"][name] = entry
        result["ig"]["total"]["published"] += len(state.get("published", []))
        result["ig"]["total"]["failed"] += len(state.get("failed", []))
        if acc:
            result["sources"]["ig_account_" + name] = "ok"
        else:
            prev_acc = ((prev.get("ig") or {}).get("accounts") or {}).get(name, {}).get("account")
            if prev_acc:
                entry["account"] = prev_acc
                entry["stale"] = True
                result["sources"]["ig_account_" + name] = "stale"
            else:
                result["sources"]["ig_account_" + name] = "unavailable"

    # Gumroad — shared, required; on failure keep stale
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
    print("accounts:", list(result["ig"]["accounts"].keys()),
          "| gumroad source:", result["sources"].get("gumroad"))


if __name__ == "__main__":
    main()
