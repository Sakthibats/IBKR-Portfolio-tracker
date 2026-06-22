"""
IBKR Portfolio Tracker — Flask backend
Requires: IBKR Client Portal Gateway running at https://localhost:5001 with an active session.
Run:  flask run   (or python app.py)
"""

import time
import urllib3
from datetime import date, datetime, timezone
from flask import Flask, jsonify, render_template
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

BASE_URL = "https://localhost:5001/v1/api"
REQUEST_TIMEOUT = 10


# ---------------------------------------------------------------------------
# IBKR gateway helpers
# ---------------------------------------------------------------------------

def ibkr_get(endpoint: str, params: dict | None = None):
    """GET request to the local IBKR Client Portal gateway."""
    try:
        resp = requests.get(
            f"{BASE_URL}{endpoint}",
            params=params,
            verify=False,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ConnectionError:
        return {"error": "gateway_unreachable"}
    except Exception as e:
        return {"error": str(e)}


def get_accounts() -> list[str]:
    data = ibkr_get("/iserver/accounts")
    if isinstance(data, dict) and "error" in data:
        return []
    return data.get("accounts", [])


def get_account_summary(account_id: str) -> dict:
    """Return key balance metrics for the account."""
    data = ibkr_get(f"/portfolio/{account_id}/summary")
    if not data or isinstance(data, dict) and "error" in data:
        return {}

    def amt(key: str) -> float:
        item = data.get(key, {})
        return item.get("amount", 0.0) if item else 0.0

    return {
        "netLiq":         amt("netliquidation"),
        "cash":           amt("totalcashvalue"),
        "unrealizedPnl":  amt("unrealizedpnl"),
        "buyingPower":    amt("buyingpower"),
        "excessLiquidity": amt("excessliquidity"),
        "maintMargin":    amt("maintmarginreq"),
    }


def get_positions(account_id: str) -> list[dict]:
    """Return all open positions (page 0; handles most portfolios)."""
    data = ibkr_get(f"/portfolio/{account_id}/positions/0")
    if not data or isinstance(data, list) and len(data) == 0:
        return []
    if isinstance(data, dict) and "error" in data:
        return []
    return data


def get_market_prices(conids: list[int]) -> dict[int, float]:
    """
    Fetch last-trade prices for a list of contract IDs via the market data
    snapshot endpoint.  IBKR uses a subscription model — the first call
    registers the subscription; a second call (after a short delay) returns
    the actual values.  Field 31 = last price.
    """
    if not conids:
        return {}

    conid_str = ",".join(str(c) for c in conids)
    prices: dict[int, float] = {}

    for attempt in range(3):
        data = ibkr_get("/iserver/marketdata/snapshot", params={"conids": conid_str, "fields": "31"})
        if isinstance(data, list):
            for item in data:
                conid = item.get("conid")
                raw = item.get("31")
                if conid and raw and raw != "Subscribing...":
                    try:
                        prices[int(conid)] = float(str(raw).replace(",", ""))
                    except (ValueError, TypeError):
                        pass

        # If we got all prices, stop early
        if len(prices) == len(conids):
            break
        if attempt < 2:
            time.sleep(0.6)

    return prices


# ---------------------------------------------------------------------------
# Options analytics helpers
# ---------------------------------------------------------------------------

def _parse_expiry(expiry_str: str) -> date | None:
    """Convert YYYYMMDD string to a date object."""
    try:
        return date(int(expiry_str[:4]), int(expiry_str[4:6]), int(expiry_str[6:8]))
    except Exception:
        return None


def _compute_option_metrics(pos: dict, underlying_price: float | None) -> dict:
    """
    Derive health metrics for a single short option position.

    For options sellers (short = negative position):
      - max_profit  = premium_per_share × |contracts| × multiplier
      - pnl_pct     = unrealized_pnl / max_profit   (how much of premium is pocketed)
      - otm_pct     = distance from strike to underlying (positive = OTM = safe)
      - status      = safe / watch / risk / expired
    """
    avg_price   = abs(pos.get("avgPrice", 0.0))   # premium per share received
    qty         = pos.get("position", 0.0)
    multiplier  = pos.get("multiplier", 100.0) or 100.0
    unreal_pnl  = pos.get("unrealizedPnl", 0.0)
    expiry_str  = pos.get("expiry", "")
    put_or_call = pos.get("putOrCall", "")
    strike      = float(pos.get("strike", 0) or 0)

    # DTE
    exp_date = _parse_expiry(expiry_str) if expiry_str else None
    dte = (exp_date - date.today()).days if exp_date else None

    # Max profit (premium received, total)
    max_profit = avg_price * abs(qty) * multiplier

    # % of premium captured so far
    pnl_pct_captured = (unreal_pnl / max_profit * 100) if max_profit else None

    # % OTM — positive means option is out of the money (healthy for seller)
    otm_pct = None
    if underlying_price and underlying_price > 0 and strike > 0:
        if put_or_call == "P":
            otm_pct = (underlying_price - strike) / underlying_price * 100
        elif put_or_call == "C":
            otm_pct = (strike - underlying_price) / underlying_price * 100

    # Health status
    if dte is not None and dte <= 0:
        status = "expired"
    elif otm_pct is not None:
        if otm_pct < 0 or (dte is not None and dte <= 5):
            status = "risk"
        elif otm_pct < 8 or (dte is not None and dte <= 14):
            status = "watch"
        else:
            status = "safe"
    else:
        # No underlying price available — flag as unknown
        status = "unknown"

    return {
        "dte":             dte,
        "maxProfit":       round(max_profit, 2),
        "pnlPctCaptured":  round(pnl_pct_captured, 1) if pnl_pct_captured is not None else None,
        "underlyingPrice": round(underlying_price, 2) if underlying_price else None,
        "otmPct":          round(otm_pct, 1) if otm_pct is not None else None,
        "status":          status,
    }


# ---------------------------------------------------------------------------
# Portfolio assembly
# ---------------------------------------------------------------------------

def build_portfolio(account_id: str) -> dict:
    """Fetch and assemble the full portfolio response."""
    positions = get_positions(account_id)
    summary   = get_account_summary(account_id)

    stocks: list[dict] = []
    options: list[dict] = []

    # Map conid → mktPrice for stock positions (used as underlying price for options)
    stock_price_map: dict[int, float] = {
        pos["conid"]: pos.get("mktPrice", 0.0)
        for pos in positions
        if pos.get("assetClass") == "STK"
    }

    # Identify option positions whose underlying is NOT in our stock holdings
    opt_positions = [p for p in positions if p.get("assetClass") == "OPT"]
    missing_conids = list({
        p["undConid"]
        for p in opt_positions
        if p.get("undConid") and p["undConid"] not in stock_price_map
    })

    # Batch-fetch live prices for underlyings we don't hold directly
    live_prices = get_market_prices(missing_conids) if missing_conids else {}
    underlying_price_map = {**stock_price_map, **live_prices}

    # Build stocks list
    for pos in positions:
        if pos.get("assetClass") != "STK":
            continue
        avg_cost = pos.get("avgCost", 0.0)
        unreal   = pos.get("unrealizedPnl", 0.0)
        pnl_pct  = (unreal / (avg_cost * pos.get("position", 1)) * 100) if avg_cost else None

        stocks.append({
            "ticker":        pos.get("ticker", pos.get("contractDesc", "")),
            "name":          pos.get("name", ""),
            "qty":           pos.get("position", 0),
            "mktPrice":      round(pos.get("mktPrice", 0.0), 4),
            "mktValue":      round(pos.get("mktValue", 0.0), 2),
            "avgCost":       round(avg_cost, 4),
            "unrealizedPnl": round(unreal, 2),
            "pnlPct":        round(pnl_pct, 2) if pnl_pct is not None else None,
        })

    # Build options list
    for pos in opt_positions:
        und_price = underlying_price_map.get(pos.get("undConid", 0))
        metrics   = _compute_option_metrics(pos, und_price)

        exp_date_obj = _parse_expiry(pos.get("expiry", ""))
        expiry_fmt   = exp_date_obj.strftime("%b %d '%y") if exp_date_obj else "—"

        options.append({
            "ticker":        pos.get("ticker", ""),
            "name":          pos.get("name", ""),
            "type":          pos.get("putOrCall", ""),           # "P" or "C"
            "strike":        float(pos.get("strike", 0) or 0),
            "expiry":        expiry_fmt,
            "qty":           int(pos.get("position", 0)),
            "premium":       round(abs(pos.get("avgPrice", 0.0)), 4),  # per share
            "currentPrice":  round(pos.get("mktPrice", 0.0), 4),
            "mktValue":      round(pos.get("mktValue", 0.0), 2),
            "unrealizedPnl": round(pos.get("unrealizedPnl", 0.0), 2),
            **metrics,
        })

    # Sort: stocks by ticker; options by status priority then ticker
    stocks.sort(key=lambda x: x["ticker"])
    status_order = {"risk": 0, "watch": 1, "unknown": 2, "safe": 3, "expired": 4}
    options.sort(key=lambda x: (status_order.get(x["status"], 9), x["ticker"]))

    return {
        "account":     summary,
        "stocks":      stocks,
        "options":     options,
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    """Quick gateway health check."""
    data = ibkr_get("/iserver/auth/status")
    if isinstance(data, dict) and "error" in data:
        return jsonify({"ok": False, "error": data["error"]}), 503
    return jsonify({
        "ok":            data.get("authenticated", False),
        "authenticated": data.get("authenticated", False),
        "connected":     data.get("connected", False),
    })


@app.route("/api/portfolio")
def api_portfolio():
    """Return the full portfolio payload."""
    accounts = get_accounts()
    if not accounts:
        return jsonify({"error": "no_accounts", "message": "No accounts found or gateway unreachable."}), 503

    portfolio = build_portfolio(accounts[0])
    return jsonify(portfolio)


if __name__ == "__main__":
    app.run(debug=True, port=5050)
