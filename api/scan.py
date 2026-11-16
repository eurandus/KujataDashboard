"""Vercel serverless function: POST /api/scan
Body: {"items":[{"name":"Animal Glue","ease":1,"npc":8}, {"id":5503}], "share":0.15, "tax":0.05, "budget":100000}
Pulls live Universalis data for Kujata + the other Elemental worlds and ranks the items.
"""
import json, math, datetime as dt
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
import requests

HOME = "Kujata"
ELEMENTAL = ["Aegis", "Atomos", "Carbuncle", "Garuda", "Gungnir", "Kujata", "Tonberry", "Typhon"]
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
                 listings=30, entries=30, statsWithin=7 * 24 * 3600 * 1000)
        items = d.get("items") or {str(chunk[0]): d}
        res.update({int(k): v for k, v in items.items()})
    return res


def min_nq(data):
    ls = [l["pricePerUnit"] for l in data.get("listings", []) if not l.get("hq")]
    return min(ls) if ls else None


def avg_sale(data):
    s = [h for h in data.get("recentHistory", []) if not h.get("hq")]
    units = sum(h["quantity"] for h in s)
    return sum(h["pricePerUnit"] * h["quantity"] for h in s) / units if units else None


def pct_rank(vals, v):
    vs = sorted(x for x in vals if x is not None)
    return 0 if v is None or not vs else sum(x <= v for x in vs) / len(vs)


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
    ids = [m["id"] for m in metas]
    with ThreadPoolExecutor(8) as ex:
        stats = dict(zip(ELEMENTAL, ex.map(lambda w: world_stats(w, ids), ELEMENTAL)))
    rows = []
    for m in metas:
        h = stats[HOME].get(m["id"], {})
        vel = h.get("nqSaleVelocity") or h.get("regularSaleVelocity") or 0
        sell = avg_sale(h) or min_nq(h)
        opts = []
        if m["npc"]:
            opts.append((float(m["npc"]), "NPC"))
        for w in ELEMENTAL:
            if w != HOME:
                p = min_nq(stats[w].get(m["id"], {}))
                if p:
                    opts.append((float(p), w))
        cost, src = min(opts) if opts else (None, "-")
        net = sell * (1 - tax) - cost if sell and cost else None
        rows.append(dict(
            id=m["id"], item=m["name"], buy_from=src, cost=cost, npc_note=m["npc_note"],
            kujata_min=min_nq(h), avg_sale=round(sell) if sell else None,
            sales_per_day=round(vel, 2), listings=h.get("listingsCount"),
            net=round(net) if net is not None else None,
            roi=round(net / cost * 100, 1) if net is not None and cost else None,
            daily_profit=round(net * vel * share) if net is not None else None,
            ease=m["ease"], last_upload=h.get("lastUploadTime")))
    col = lambda k: [r[k] for r in rows]
    comp = [-(r["listings"] or 0) for r in rows]
    for r, c in zip(rows, comp):
        r["score"] = round(100 * (
            W["velocity"] * pct_rank(col("sales_per_day"), r["sales_per_day"]) +
            W["daily_profit"] * pct_rank(col("daily_profit"), r["daily_profit"]) +
            W["ease"] * r["ease"] + W["competition"] * pct_rank(comp, c) +
            W["roi"] * pct_rank(col("roi"), r["roi"])), 1)
    rows.sort(key=lambda r: -r["score"])
    winners = [r for r in rows if r["net"] and r["net"] > 0][:8]
    for r in rows:
        r["start_qty"] = 0
    for r in winners:
        three_days = math.ceil(r["sales_per_day"] * share * 3)
        r["start_qty"] = max(0, min(three_days, int(budget / max(len(winners), 1) // r["cost"])))
    return dict(rows=rows, problems=problems, tax=tax, share=share,
                fetched=dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"))


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200, {"ok": True, "hint": "POST items to this endpoint"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            self._send(200, scan(json.loads(self.rfile.read(n) or b"{}")))
        except Exception as e:  # surface the reason to the UI
            self._send(500, {"error": str(e)})
