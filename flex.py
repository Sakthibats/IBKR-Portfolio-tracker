"""
IBKR Flex Web Service client + options trade history store.

Fetches an Activity Flex Query (Trades section) via the two-step Flex API,
caches executions in a local SQLite database, and derives option "plays"
(round trips) plus summary statistics from them.

Environment:
    IBKR_FLEX_TOKEN     – Flex Web Service token (required, from .env)
    IBKR_FLEX_QUERY_ID  – Activity Flex Query ID (default: 1570999)
"""

import hashlib
import os
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime

import requests

FLEX_BASE = "https://gdcdyn.interactivebrokers.com/Universal/servlet"
SEND_URL = f"{FLEX_BASE}/FlexStatementService.SendRequest"
GET_URL = f"{FLEX_BASE}/FlexStatementService.GetStatement"
FLEX_VERSION = "3"
# IBKR rejects requests without a browser-ish User-Agent
HEADERS = {"User-Agent": "Mozilla/5.0 (IBKR-Portfolio-Tracker)"}

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flex_trades.db")
MANUAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Manual")


class FlexError(Exception):
    """Raised for any Flex Web Service failure with a user-readable message."""


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _flex_config() -> tuple[str, str]:
    token = os.environ.get("IBKR_FLEX_TOKEN", "").strip()
    query_id = os.environ.get("IBKR_FLEX_QUERY_ID", "1570999").strip()
    if not token:
        raise FlexError("IBKR_FLEX_TOKEN is not set. Add it to your .env file.")
    return token, query_id


def _save_statement_xml(xml_text: str) -> str:
    """Save a fetched Flex statement XML into the Manual folder; returns the path."""
    os.makedirs(MANUAL_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(MANUAL_DIR, f"flex_statement_{stamp}.xml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(xml_text)
    return path


def fetch_flex_statement() -> str:
    """Run the two-step Flex fetch and return the statement XML as text."""
    token, query_id = _flex_config()

    # Step 1: request report generation
    resp = requests.get(
        SEND_URL,
        params={"t": token, "q": query_id, "v": FLEX_VERSION},
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    if root.findtext("Status") != "Success":
        raise FlexError(
            f"Flex request failed: {root.findtext('ErrorMessage') or resp.text[:200]}"
        )
    ref_code = root.findtext("ReferenceCode")

    # Step 2: poll for the generated statement
    last_err = "timed out waiting for statement"
    for _ in range(12):
        resp = requests.get(
            GET_URL,
            params={"t": token, "q": ref_code, "v": FLEX_VERSION},
            headers=HEADERS,
            timeout=60,
        )
        resp.raise_for_status()
        text = resp.text
        if "<FlexQueryResponse" in text:
            _save_statement_xml(text)
            return text
        try:
            err_root = ET.fromstring(text)
            code = err_root.findtext("ErrorCode") or ""
            last_err = err_root.findtext("ErrorMessage") or text[:200]
            # 1019 = statement generation in progress
            if code not in ("1019", "1021", "1001"):
                raise FlexError(f"Flex statement failed: {last_err}")
        except ET.ParseError:
            last_err = text[:200]
        time.sleep(5)

    raise FlexError(f"Flex statement not ready: {last_err}")


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

def _f(el: ET.Element, attr: str) -> float:
    raw = el.get(attr, "")
    try:
        return float(raw)
    except (ValueError, TypeError):
        return 0.0


def parse_executions(xml_text: str) -> list[dict]:
    """Extract option and stock executions (<Trade> elements, OPT/FOP/STK)."""
    root = ET.fromstring(xml_text)
    rows: list[dict] = []
    seen_keys: dict[str, int] = {}

    for tr in root.iter("Trade"):
        if tr.get("assetCategory") not in ("OPT", "FOP", "STK"):
            continue

        row = {
            "asset_category": tr.get("assetCategory", ""),
            "currency":      tr.get("currency", "") or "USD",
            "symbol":        tr.get("symbol", ""),
            "underlying":    tr.get("underlyingSymbol", "") or tr.get("symbol", "").split()[0],
            "conid":         tr.get("conid", ""),
            "description":   tr.get("description", ""),
            "expiry":        tr.get("expiry", ""),                  # YYYYMMDD
            "strike":        _f(tr, "strike"),
            "put_call":      tr.get("putCall", ""),                 # "P" / "C"
            "multiplier":    _f(tr, "multiplier")
                             or (1.0 if tr.get("assetCategory") == "STK" else 100.0),
            "trade_date":    tr.get("tradeDate", ""),               # YYYYMMDD
            "date_time":     tr.get("dateTime", "") or tr.get("tradeDate", ""),
            "buy_sell":      tr.get("buySell", ""),                 # "BUY" / "SELL"
            "quantity":      _f(tr, "quantity"),                    # signed contracts
            "trade_price":   _f(tr, "tradePrice"),
            "proceeds":      _f(tr, "proceeds"),
            "commission":    _f(tr, "ibCommission"),
            "net_cash":      _f(tr, "netCash"),
            "open_close":    tr.get("openCloseIndicator", ""),      # "O" / "C" / "C;O"
            "realized_pnl":  _f(tr, "fifoPnlRealized"),
            "cost_basis":    _f(tr, "cost"),
            "notes":         tr.get("notes", ""),                   # "A", "Ep", "Ex", ...
            "transaction_type": tr.get("transactionType", ""),
        }
        # Some queries omit tradePrice; recover the per-share price from
        # proceeds so premium math still works.
        if not row["trade_price"] and row["quantity"] and row["proceeds"]:
            row["trade_price"] = abs(row["proceeds"]) / (
                abs(row["quantity"]) * row["multiplier"]
            )

        # Stable synthetic ID for dedup across syncs (tradeID isn't guaranteed
        # to be in the query, so hash the identifying attributes). Partial
        # fills can be byte-identical, so suffix an occurrence counter —
        # stable across syncs because every sync re-fetches the full window.
        trade_id = tr.get("tradeID") or tr.get("transactionID")
        if not trade_id:
            key = "|".join(str(row[k]) for k in (
                "conid", "date_time", "buy_sell", "quantity", "trade_price", "proceeds"))
            seq = seen_keys.get(key, 0)
            seen_keys[key] = seq + 1
            trade_id = hashlib.sha1(f"{key}#{seq}".encode()).hexdigest()[:16]
        row["id"] = str(trade_id)
        rows.append(row)

    return rows


# ---------------------------------------------------------------------------
# SQLite store
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    id            TEXT PRIMARY KEY,
    asset_category TEXT, currency TEXT,
    symbol        TEXT, underlying TEXT, conid TEXT, description TEXT,
    expiry        TEXT, strike REAL, put_call TEXT, multiplier REAL,
    trade_date    TEXT, date_time TEXT, buy_sell TEXT,
    quantity      REAL, trade_price REAL, proceeds REAL,
    commission    REAL, net_cash REAL, open_close TEXT,
    realized_pnl  REAL, cost_basis REAL, notes TEXT, transaction_type TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

_COLS = [
    "id", "asset_category", "currency", "symbol", "underlying", "conid",
    "description", "expiry", "strike",
    "put_call", "multiplier", "trade_date", "date_time", "buy_sell", "quantity",
    "trade_price", "proceeds", "commission", "net_cash", "open_close",
    "realized_pnl", "cost_basis", "notes", "transaction_type",
]


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Migrate pre-stock caches (missing asset_category): drop and let the
    # next sync/import repopulate — the table is only a cache.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(executions)")}
    if cols and "asset_category" not in cols:
        conn.execute("DROP TABLE executions")
    conn.executescript(_SCHEMA)
    return conn


def sync_trades() -> dict:
    """Fetch the Flex statement and upsert executions. Returns counts."""
    return _store_executions(parse_executions(fetch_flex_statement()))


def import_xml_file(path: str) -> dict:
    """Import a manually exported Flex statement XML into the trade cache."""
    with open(path, encoding="utf-8") as f:
        return _store_executions(parse_executions(f.read()))


def _store_executions(rows: list[dict]) -> dict:
    with _db() as conn:
        before = conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
        conn.executemany(
            f"INSERT OR REPLACE INTO executions ({','.join(_COLS)}) "
            f"VALUES ({','.join('?' for _ in _COLS)})",
            [[r[c] for c in _COLS] for r in rows],
        )
        after = conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_sync', ?)", (now,)
        )

    return {"fetched": len(rows), "new": after - before, "total": after, "lastSync": now}


def load_executions() -> list[dict]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM executions ORDER BY date_time").fetchall()
    return [dict(r) for r in rows]


def last_sync() -> str | None:
    with _db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
    return row["value"] if row else None


# ---------------------------------------------------------------------------
# Play pairing (round trips)
# ---------------------------------------------------------------------------

def _parse_yyyymmdd(s: str) -> date | None:
    s = (s or "")[:8]
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except (ValueError, IndexError):
        return None


def _fmt_date(d: date | None) -> str | None:
    return d.strftime("%b %d '%y") if d else None


def _weighted_price(legs: list[dict]) -> float:
    total_qty = sum(abs(l["quantity"]) for l in legs)
    if not total_qty:
        return 0.0
    return sum(abs(l["quantity"]) * l["trade_price"] for l in legs) / total_qty


def _notes_tokens(legs: list[dict]) -> set[str]:
    tokens: set[str] = set()
    for l in legs:
        tokens.update(t.strip() for t in (l["notes"] or "").split(";") if t.strip())
    return tokens


def build_plays(executions: list[dict]) -> list[dict]:
    """
    Group executions per contract (underlying/expiry/strike/right) and split
    into plays: a play opens when the net position leaves 0 and closes when
    it returns to 0. Handles partial fills and repeated plays on a contract.
    """
    by_contract: dict[tuple, list[dict]] = {}
    for ex in executions:
        if ex.get("asset_category", "OPT") not in ("OPT", "FOP"):
            continue
        key = (ex["underlying"], ex["expiry"], ex["strike"], ex["put_call"])
        by_contract.setdefault(key, []).append(ex)

    plays: list[dict] = []
    today = date.today()

    for key, execs in by_contract.items():
        execs.sort(key=lambda e: e["date_time"])
        current: list[dict] = []
        pos = 0.0
        for ex in execs:
            current.append(ex)
            pos += ex["quantity"]
            if abs(pos) < 1e-9 and current:
                plays.append(_make_play(key, current, closed=True, today=today))
                current, pos = [], 0.0
        if current:
            plays.append(_make_play(key, current, closed=False, today=today))

    _detect_rolls(plays)
    plays.sort(key=lambda p: p["openDateRaw"] or "", reverse=True)
    return plays


def _make_play(key: tuple, legs: list[dict], closed: bool, today: date) -> dict:
    underlying, expiry, strike, put_call = key
    init_sign = 1 if legs[0]["quantity"] > 0 else -1
    open_legs = [l for l in legs if (l["quantity"] > 0) == (init_sign > 0)]
    close_legs = [l for l in legs if l not in open_legs]

    direction = "sold" if init_sign < 0 else "bought"
    contracts = int(round(sum(abs(l["quantity"]) for l in open_legs)))
    multiplier = open_legs[0]["multiplier"] or 100.0

    open_price = _weighted_price(open_legs)
    close_price = _weighted_price(close_legs) if close_legs else None

    open_date = _parse_yyyymmdd(open_legs[0]["trade_date"])
    close_date = _parse_yyyymmdd(close_legs[-1]["trade_date"]) if close_legs else None
    exp_date = _parse_yyyymmdd(expiry)

    commissions = sum(l["commission"] for l in legs)
    premium_total = open_price * contracts * multiplier  # gross, at open

    # Realized P&L: prefer IBKR's FIFO number; fall back to summed cash flow
    realized = sum(l["realized_pnl"] for l in legs)
    if closed and abs(realized) < 1e-9:
        realized = sum(l["proceeds"] + l["commission"] for l in legs)

    # Outcome
    notes = _notes_tokens(close_legs)
    if closed:
        if "A" in notes:
            outcome = "assigned"
        elif "Ex" in notes:
            outcome = "exercised"
        elif "Ep" in notes:
            outcome = "expired"
        else:
            outcome = "closed"
    elif exp_date and exp_date < today:
        # No closing execution but past expiry: the query predates the expiry
        # row or expirations aren't reported — treat as expired worthless.
        outcome = "expired"
        closed = True
        close_date = exp_date
        close_price = 0.0
        realized = sum(l["proceeds"] + l["commission"] for l in legs)
    else:
        outcome = "open"

    days_held = (close_date - open_date).days if (open_date and close_date) else None
    dte_at_open = (exp_date - open_date).days if (open_date and exp_date) else None
    pnl_pct = (realized / abs(premium_total) * 100) if (closed and premium_total) else None

    return {
        "underlying":   underlying,
        "type":         put_call,
        "strike":       strike,
        "expiry":       _fmt_date(exp_date),
        "expiryRaw":    expiry,
        "direction":    direction,
        "contracts":    contracts,
        "openDate":     _fmt_date(open_date),
        "openDateRaw":  open_date.isoformat() if open_date else None,
        "closeDate":    _fmt_date(close_date),
        "closeDateRaw": close_date.isoformat() if close_date else None,
        "daysHeld":     days_held,
        "dteAtOpen":    dte_at_open,
        "openPrice":    round(open_price, 4),
        "closePrice":   round(close_price, 4) if close_price is not None else None,
        "premiumTotal": round(premium_total, 2),
        "commissions":  round(commissions, 2),
        "realizedPnl":  round(realized, 2) if closed else None,
        "pnlPct":       round(pnl_pct, 1) if pnl_pct is not None else None,
        "outcome":      outcome,
        "rolled":       False,
        "legs":         len(legs),
    }


def _detect_rolls(plays: list[dict]) -> None:
    """Tag close+open on the same underlying/right/day as a roll."""
    opens: dict[tuple, list[dict]] = {}
    for p in plays:
        if p["openDateRaw"]:
            opens.setdefault((p["underlying"], p["type"], p["openDateRaw"]), []).append(p)

    for p in plays:
        if p["outcome"] != "closed" or not p["closeDateRaw"]:
            continue
        for candidate in opens.get((p["underlying"], p["type"], p["closeDateRaw"]), []):
            if candidate is not p and (
                candidate["expiryRaw"] != p["expiryRaw"]
                or candidate["strike"] != p["strike"]
            ):
                p["rolled"] = True
                candidate["rolled"] = True
                break


# ---------------------------------------------------------------------------
# Stock trades: blotter + FIFO realized sells
# ---------------------------------------------------------------------------

_TAG_MAP = {"IA": "auto", "FP": "fractional", "A": "assignment", "RP": "reinvest"}


def _tags(notes: str) -> list[str]:
    tokens = [t.strip() for t in (notes or "").split(";")]
    return [_TAG_MAP[t] for t in tokens if t in _TAG_MAP]


def build_stock_trades(executions: list[dict]) -> dict:
    """
    Return the stock blotter (every execution) and realized sells.

    Sells are matched to buy lots FIFO within the statement window; a sell
    whose lots predate the window is flagged (coverage partial/unknown) and
    carries no P&L for the unmatched share. Cost basis and proceeds use
    net cash, so commissions are baked into P&L.
    """
    stk = [e for e in executions if e.get("asset_category") == "STK"]

    blotter: list[dict] = []
    sells: list[dict] = []
    by_symbol: dict[tuple, list[dict]] = {}
    for ex in sorted(stk, key=lambda e: e["date_time"]):
        by_symbol.setdefault((ex["symbol"], ex["currency"]), []).append(ex)

        d = _parse_yyyymmdd(ex["trade_date"])
        blotter.append({
            "symbol":     ex["symbol"],
            "name":       ex["description"],
            "currency":   ex["currency"],
            "date":       _fmt_date(d),
            "dateRaw":    d.isoformat() if d else None,
            "side":       ex["buy_sell"],
            "qty":        ex["quantity"],
            "price":      round(ex["trade_price"], 4),
            "value":      round(ex["proceeds"], 2),
            "commission": round(ex["commission"], 2),
            "tags":       _tags(ex["notes"]),
        })

    for (symbol, currency), execs in by_symbol.items():
        lots: list[dict] = []  # FIFO queue of open buy lots
        for ex in execs:
            qty = ex["quantity"]
            d = _parse_yyyymmdd(ex["trade_date"])
            if qty > 0:
                # net cost per share, fees in (net_cash is negative for buys)
                cost_px = (-ex["net_cash"] / qty) if ex["net_cash"] else ex["trade_price"]
                lots.append({"qty": qty, "px": cost_px, "date": d})
                continue
            if qty == 0:
                continue

            sell_qty = -qty
            net_px = (ex["net_cash"] / sell_qty) if ex["net_cash"] else ex["trade_price"]
            matched_qty = 0.0
            matched_cost = 0.0
            weighted_days = 0.0
            remaining = sell_qty
            while remaining > 1e-9 and lots:
                lot = lots[0]
                take = min(lot["qty"], remaining)
                matched_qty += take
                matched_cost += take * lot["px"]
                if d and lot["date"]:
                    weighted_days += take * (d - lot["date"]).days
                lot["qty"] -= take
                remaining -= take
                if lot["qty"] <= 1e-9:
                    lots.pop(0)

            avg_cost = matched_cost / matched_qty if matched_qty else None
            realized = matched_qty * (net_px - avg_cost) if matched_qty else None
            coverage = ("full" if remaining <= 1e-9
                        else "partial" if matched_qty else "unknown")
            sells.append({
                "symbol":    symbol,
                "currency":  currency,
                "date":      _fmt_date(d),
                "dateRaw":   d.isoformat() if d else None,
                "qty":       round(sell_qty, 4),
                "sellPrice": round(ex["trade_price"], 4),
                "avgCost":   round(avg_cost, 4) if avg_cost is not None else None,
                "realizedPnl": round(realized, 2) if realized is not None else None,
                "pnlPct":    round((net_px - avg_cost) / avg_cost * 100, 1)
                             if avg_cost else None,
                "daysHeld":  round(weighted_days / matched_qty, 1) if matched_qty else None,
                "coverage":  coverage,
                "matchedQty": round(matched_qty, 4),
                "tags":      _tags(ex["notes"]),
            })

    blotter.sort(key=lambda b: b["dateRaw"] or "", reverse=True)
    sells.sort(key=lambda s: s["dateRaw"] or "", reverse=True)
    return {"blotter": blotter, "sells": sells}


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------

def compute_stats(plays: list[dict]) -> dict:
    closed = [p for p in plays if p["outcome"] != "open" and p["realizedPnl"] is not None]
    sold = [p for p in plays if p["direction"] == "sold"]

    total_realized = sum(p["realizedPnl"] for p in closed)
    premium_collected = sum(p["premiumTotal"] for p in sold)
    wins = [p for p in closed if p["realizedPnl"] > 0]

    def avg(vals: list) -> float | None:
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    # Outcome distribution (closed plays only)
    outcomes: dict[str, int] = {}
    for p in closed:
        outcomes[p["outcome"]] = outcomes.get(p["outcome"], 0) + 1

    # Per-ticker breakdown
    by_ticker: dict[str, dict] = {}
    for p in plays:
        t = by_ticker.setdefault(p["underlying"], {
            "ticker": p["underlying"], "plays": 0, "premium": 0.0,
            "realizedPnl": 0.0, "wins": 0, "closed": 0,
        })
        t["plays"] += 1
        if p["direction"] == "sold":
            t["premium"] += p["premiumTotal"]
        if p["realizedPnl"] is not None:
            t["realizedPnl"] += p["realizedPnl"]
            t["closed"] += 1
            if p["realizedPnl"] > 0:
                t["wins"] += 1
    ticker_rows = sorted(by_ticker.values(), key=lambda t: -t["realizedPnl"])
    for t in ticker_rows:
        t["premium"] = round(t["premium"], 2)
        t["realizedPnl"] = round(t["realizedPnl"], 2)
        t["winRate"] = round(t["wins"] / t["closed"] * 100, 0) if t["closed"] else None

    # Monthly aggregates: premium by open month, realized P&L by close month
    monthly: dict[str, dict] = {}
    for p in plays:
        if p["direction"] == "sold" and p["openDateRaw"]:
            m = p["openDateRaw"][:7]
            monthly.setdefault(m, {"month": m, "premium": 0.0, "realizedPnl": 0.0})
            monthly[m]["premium"] += p["premiumTotal"]
        if p["realizedPnl"] is not None and p["closeDateRaw"]:
            m = p["closeDateRaw"][:7]
            monthly.setdefault(m, {"month": m, "premium": 0.0, "realizedPnl": 0.0})
            monthly[m]["realizedPnl"] += p["realizedPnl"]
    monthly_rows = sorted(monthly.values(), key=lambda m: m["month"])
    for m in monthly_rows:
        m["premium"] = round(m["premium"], 2)
        m["realizedPnl"] = round(m["realizedPnl"], 2)

    # Puts vs calls (closed)
    put_call = {}
    for right, label in (("P", "puts"), ("C", "calls")):
        subset = [p for p in closed if p["type"] == right]
        put_call[label] = {
            "plays": len(subset),
            "realizedPnl": round(sum(p["realizedPnl"] for p in subset), 2),
            "winRate": round(
                len([p for p in subset if p["realizedPnl"] > 0]) / len(subset) * 100, 0
            ) if subset else None,
        }

    ranked = sorted(closed, key=lambda p: p["realizedPnl"])

    return {
        "headline": {
            "premiumCollected": round(premium_collected, 2),
            "realizedPnl":      round(total_realized, 2),
            "winRate":          round(len(wins) / len(closed) * 100, 0) if closed else None,
            "totalPlays":       len(plays),
            "closedPlays":      len(closed),
            "openPlays":        len(plays) - len(closed),
            "avgPremium":       avg([p["premiumTotal"] for p in sold]),
            "avgDaysHeld":      avg([p["daysHeld"] for p in closed]),
            "avgDteAtOpen":     avg([p["dteAtOpen"] for p in plays]),
        },
        "outcomes":    outcomes,
        "byTicker":    ticker_rows,
        "monthly":     monthly_rows,
        "putCall":     put_call,
        "bestTrades":  ranked[-5:][::-1],
        "worstTrades": ranked[:5],
    }
