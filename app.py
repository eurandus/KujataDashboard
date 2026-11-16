"""Flask app for Vercel. POST /api/scan
Body: {"items":[{"name":"Animal Glue","ease":1,"npc":8}, {"id":5503}], "share":0.15, "tax":0.05, "budget":100000}
Pulls live Universalis data for Kujata + the other Elemental worlds and ranks the items.
"""
import json, math, time, datetime as dt
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from flask import Flask, request, jsonify, Response
import requests

HOME = "Kujata"
ENTRIES = 100
UNI = "https://universalis.app/api/v2"
XIV = "https://v2.xivapi.com/api"
W = dict(velocity=0.30, daily_profit=0.25, ease=0.20, competition=0.15, roi=0.10)
S = requests.Session()
S.headers["User-Agent"] = "kujata-dashboard/0.1"


def jget(url, **params):
    last = None
    for _ in range(2):
        try:
            r = S.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            last = RuntimeError(f"{r.status_code} from {url}")
        except requests.RequestException as e:
            last = e
    raise last


def resolve(item):
    """-> dict(id, name, ease, npc, npc_note, warn)"""
    out = dict(name=item.get("name") or "", ease=float(item.get("ease", 0.5)),
               npc=item.get("npc"), npc_note="you entered" if item.get("npc") is not None else "", warn=None)
    if item.get("id"):
        out["id"] = int(item["id"])
        out["name"] = out["name"] or f"id {out['id']}"
        return out
    try:
        d = jget(f"{XIV}/search", sheets="Item", query=f'Name="{out["name"]}"',
                 fields="Name,PriceMid,IsUntradable")
    except Exception as e:
        out["id"], out["warn"] = None, f"lookup failed ({e})"
        return out
    for hit in d.get("results", []):
        f = hit["fields"]
        if f.get("Name", "").lower() == out["name"].lower():
            if f.get("IsUntradable"):
                out["id"], out["warn"] = None, "untradable"
                return out
            out["id"] = hit["row_id"]
            if out["npc"] is None and f.get("PriceMid"):
                out["npc"] = f["PriceMid"]
                out["npc_note"] = "game data price; confirm a vendor sells it"
            return out
    out["id"], out["warn"] = None, "name not found"
    return out


def world_stats(world, ids):
    res = {}
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        d = jget(f"{UNI}/{world}/{','.join(map(str, chunk))}",
                 listings=30, entries=ENTRIES, statsWithin=7 * 24 * 3600 * 1000)
        items = d.get("items") or {str(chunk[0]): d}
        res.update({int(k): v for k, v in items.items()})
    return res


def min_nq(data):
    ls = [l["pricePerUnit"] for l in data.get("listings", []) if not l.get("hq")]
    return min(ls) if ls else None


def pct_rank(vals, v):
    vs = sorted(x for x in vals if x is not None)
    return 0 if v is None or not vs else sum(x <= v for x in vs) / len(vs)


def freshness(last_ms, now):
    if not last_ms:
        return "No data", None
    age_h = (now * 1000 - last_ms) / 3.6e6
    if age_h > 168:
        return "Very stale", round(age_h)
    if age_h > 48:
        return "Stale", round(age_h)
    return "Fresh", round(age_h)


def scan(body):
    share = min(max(float(body.get("share", 0.15)), 0.01), 1)
    tax = min(max(float(body.get("tax", 0.05)), 0), 0.2)
    budget = float(body.get("budget", 100000))
    raw = (body.get("items") or [])[:60]
    with ThreadPoolExecutor(8) as ex:
        metas = list(ex.map(resolve, raw))
    problems = [f"{m['name']}: {m['warn']}" for m in metas if m["warn"]]
    metas = [m for m in metas if m.get("id")]
    if not metas:
        return dict(rows=[], problems=problems, fetched=None)
    stats = world_stats(HOME, [m["id"] for m in metas])
    now = time.time()
    cutoff = now - 7 * 86400
    rows = []
    for m in metas:
        h = stats.get(m["id"], {})
        hist = [x for x in h.get("recentHistory", []) if not x.get("hq")]
        week = [x for x in hist if x.get("timestamp", 0) >= cutoff]
        sold_7d = sum(x["quantity"] for x in week)
        days_sold = len({int(x["timestamp"] // 86400) for x in week})
        capped = len(h.get("recentHistory", [])) >= ENTRIES and \
            min((x.get("timestamp", 0) for x in h["recentHistory"]), default=0) >= cutoff
        vel = (h.get("nqSaleVelocity") or sold_7d / 7) if capped else sold_7d / 7
        if sold_7d == 0:
            vel = 0
        avg = sum(x["pricePerUnit"] * x["quantity"] for x in week) / sold_7d if sold_7d else None
        sell = avg or min_nq(h)
        cost = float(m["npc"]) if m["npc"] else None
        net = sell * (1 - tax) - cost if sell and cost else None
        listings = h.get("listingsCount") or 0
        eff_share = min(share, 1 / (listings + 1))      # you cannot beat your fair share by much
        real_units = vel * eff_share
        fresh, age_h = freshness(h.get("lastUploadTime"), now)
        rows.append(dict(
            id=m["id"], item=m["name"], cost=cost, npc_note=m["npc_note"],
            kujata_min=min_nq(h), avg_sale=round(avg) if avg else None,
            sold_7d=sold_7d, days_sold=days_sold, sales_per_day=round(vel, 2), listings=listings,
            net=round(net) if net is not None else None,
            roi=round(net / cost * 100, 1) if net is not None and cost else None,
            real_units=round(real_units, 2),
            daily_profit=round(net * real_units) if net is not None else None,
            ease=m["ease"], data=fresh, data_age_h=age_h))
    col = lambda k: [r[k] for r in rows]
    comp = [-r["listings"] for r in rows]
    for r, c in zip(rows, comp):
        r["score"] = round(100 * (
            W["velocity"] * pct_rank(col("sales_per_day"), r["sales_per_day"]) +
            W["daily_profit"] * pct_rank(col("daily_profit"), r["daily_profit"]) +
            W["ease"] * r["ease"] + W["competition"] * pct_rank(comp, c) +
            W["roi"] * pct_rank(col("roi"), r["roi"])), 1)
    # rank by realizable daily profit (score only breaks ties)
    rows.sort(key=lambda r: (r["daily_profit"] is None, -(r["daily_profit"] or 0), -r["score"]))
    buyable = [r for r in rows if r["net"] and r["net"] > 0 and r["sold_7d"] > 0 and r["real_units"] > 0][:8]
    for r in rows:
        r["start_qty"] = 0
    for r in buyable:
        three_days = math.ceil(r["real_units"] * 3)
        r["start_qty"] = max(0, min(three_days, int(budget / len(buyable) // r["cost"])))
    return dict(rows=rows, problems=problems, tax=tax, share=share,
                fetched=dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"))


app = Flask(__name__)
HERE = Path(__file__).parent


@app.get("/")
def home():
    for p in (HERE / "public" / "index.html", HERE / "index.html"):
        if p.exists():
            return Response(p.read_text(encoding="utf-8"), mimetype="text/html")
    return "index.html missing", 404


@app.post("/api/scan")
def api_scan():
    try:
        out = jsonify(scan(request.get_json(force=True, silent=True) or {}))
    except Exception as e:  # show the reason in the UI
        return jsonify(error=str(e)), 500
    out.headers["Cache-Control"] = "no-store"
    return out
