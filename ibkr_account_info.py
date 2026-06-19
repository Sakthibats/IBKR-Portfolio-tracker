"""
IBKR Client Portal API - Account Info Extractor
Requires: Gateway running at https://localhost:5001 and you already logged in via browser.
"""

import requests
import json
import urllib3

# Suppress SSL warnings for self-signed cert used by the local gateway
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://localhost:5001/v1/api"


def get(endpoint: str) -> dict | list | None:
    """Make a GET request to the gateway."""
    url = f"{BASE_URL}{endpoint}"
    try:
        resp = requests.get(url, verify=False, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        print(f"  HTTP error on {endpoint}: {e}")
    except requests.exceptions.ConnectionError:
        print("  Could not connect to gateway. Is it running at https://localhost:5001?")
    except Exception as e:
        print(f"  Error on {endpoint}: {e}")
    return None


def check_auth_status() -> bool:
    """Confirm the gateway session is authenticated."""
    print("=== Auth Status ===")
    data = get("/iserver/auth/status")
    if not data:
        return False
    authenticated = data.get("authenticated", False)
    connected = data.get("connected", False)
    competing = data.get("competing", False)
    print(f"  Authenticated : {authenticated}")
    print(f"  Connected     : {connected}")
    print(f"  Competing     : {competing}")
    if not authenticated:
        print("\n  ⚠  Session not authenticated. Please log in at https://localhost:5001")
    return authenticated


def get_accounts() -> list[str]:
    """Return all account IDs under this login."""
    print("\n=== Accounts ===")
    data = get("/iserver/accounts")
    if not data:
        return []
    accounts = data.get("accounts", [])
    selected = data.get("selectedAccount", "")
    print(f"  Accounts       : {accounts}")
    print(f"  Selected       : {selected}")
    return accounts


def get_portfolio_accounts() -> list[dict]:
    """Return portfolio-level account objects with alias and type info."""
    print("\n=== Portfolio Accounts ===")
    data = get("/portfolio/accounts")
    if not data:
        return []
    for acct in data:
        acct_id   = acct.get("id", "")
        acct_type = acct.get("type", "")
        alias     = acct.get("alias", "")
        currency  = acct.get("currency", "")
        print(f"  {acct_id:>15}  type={acct_type}  alias={alias!r}  currency={currency}")
    return data


def get_account_summary(account_id: str) -> None:
    """Print a summary of balances and key metrics for one account."""
    print(f"\n=== Account Summary: {account_id} ===")
    data = get(f"/portfolio/{account_id}/summary")
    if not data:
        return

    # Key fields we care about
    keys_of_interest = [
        "netliquidation",
        "totalcashvalue",
        "settledcash",
        "buyingpower",
        "grosspositionvalue",
        "unrealizedpnl",
        "realizedpnl",
        "equitywithloanvalue",
        "maintmarginreq",
        "initmarginreq",
        "excessliquidity",
    ]

    for key in keys_of_interest:
        item = data.get(key)
        if item is None:
            continue
        amount   = item.get("amount", "N/A")
        currency = item.get("currency", "")
        label    = item.get("name", key)
        print(f"  {label:<35} {amount:>15,.2f}  {currency}")


def get_account_ledger(account_id: str) -> None:
    """Print cash ledger balances broken down by currency."""
    print(f"\n=== Account Ledger (Cash): {account_id} ===")
    data = get(f"/portfolio/{account_id}/ledger")
    if not data:
        return
    for currency, ledger in data.items():
        cash     = ledger.get("cashbalance", 0)
        net_div  = ledger.get("dividends", 0)
        realized = ledger.get("realizedpnl", 0)
        unrealized = ledger.get("unrealizedpnl", 0)
        print(f"  [{currency}]  Cash={cash:>15,.2f}  "
              f"Dividends={net_div:>10,.2f}  "
              f"RealizedPnL={realized:>12,.2f}  "
              f"UnrealizedPnL={unrealized:>12,.2f}")


def get_positions(account_id: str) -> None:
    """Print open positions for the account."""
    print(f"\n=== Positions: {account_id} ===")
    data = get(f"/portfolio/{account_id}/positions/0")
    if not data:
        print("  No positions or error.")
        return
    if not data:
        print("  No open positions.")
        return
    header = f"  {'Ticker':<10} {'Description':<30} {'Pos':>10} {'Mkt Price':>12} {'Mkt Value':>14} {'Avg Cost':>12} {'Unreal P&L':>14}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for pos in data:
        ticker   = pos.get("ticker", "")
        desc     = pos.get("name", "")[:30]
        position = pos.get("position", 0)
        mkt_px   = pos.get("mktPrice", 0)
        mkt_val  = pos.get("mktValue", 0)
        avg_cost = pos.get("avgCost", 0)
        unreal   = pos.get("unrealizedPnl", 0)
        print(f"  {ticker:<10} {desc:<30} {position:>10,.4f} {mkt_px:>12,.4f} {mkt_val:>14,.2f} {avg_cost:>12,.4f} {unreal:>14,.2f}")


def save_raw_data(account_id: str, filename: str = "ibkr_raw_data.json") -> None:
    """Dump all raw API responses to a JSON file for inspection."""
    raw = {
        "auth_status"       : get("/iserver/auth/status"),
        "accounts"          : get("/iserver/accounts"),
        "portfolio_accounts": get("/portfolio/accounts"),
        "summary"           : get(f"/portfolio/{account_id}/summary"),
        "ledger"            : get(f"/portfolio/{account_id}/ledger"),
        "positions"         : get(f"/portfolio/{account_id}/positions/0"),
    }
    with open(filename, "w") as f:
        json.dump(raw, f, indent=2)
    print(f"\n  Raw data saved to {filename}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("IBKR Client Portal API — Account Info Extractor")
    print("=" * 55)

    if not check_auth_status():
        print("\nPlease log in first, then re-run this script.")
        raise SystemExit(1)

    # Fetch account list
    account_ids = get_accounts()
    portfolio_accounts = get_portfolio_accounts()

    if not account_ids:
        print("\nNo accounts found. Exiting.")
        raise SystemExit(1)

    # Use the first account for detailed info (loop if you have multiple)
    primary = account_ids[0]

    get_account_summary(primary)
    get_account_ledger(primary)
    get_positions(primary)

    # Optionally save everything to JSON
    save_raw_data(primary)

    print("\nDone.")
