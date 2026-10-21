# IBKR-Portfolio-tracker

This project aims to perform experiments on IBKR API and its capabilities for personal finance tracking and ease of research.


## Pages

- **Portfolio** (`/`) — live positions via the Client Portal Gateway (`https://localhost:5001`)
- **History** (`/history`) — historical option plays (round trips) from the last 365 days
- **Stats** (`/stats`) — summary statistics over those plays: premium collected, win rate, outcomes, per-ticker breakdown

## Options trade history (Flex Query)

History and Stats are powered by the IBKR Flex Web Service, not the gateway:

1. In IBKR Client Portal: Performance & Reports → Flex Queries → create an
   **Activity Flex Query** with the **Trades → Executions** section
   (period: Last 365 Calendar Days, format XML).
2. On the same page, open **Flex Web Service Configuration** and generate a token.
3. Create a `.env` file (gitignored):

   ```
   IBKR_FLEX_TOKEN=<your token>
   IBKR_FLEX_QUERY_ID=<your query id>
   ```

4. Run the app and click **Sync** on the History or Stats page. Executions are
   cached in `flex_trades.db` (SQLite, gitignored), so history accumulates
   locally beyond the rolling 365-day window.

## Portfolio snapshots (offline + Cloudflare)

Every successful Portfolio refresh is saved to `flex_trades.db`
(`portfolio_snapshots`). If the gateway is down or logged out, the local page
shows the last snapshot with a banner instead of an error.

The same snapshot can be published to a free Cloudflare Pages site, so it is
readable from any device while your laptop is off. It is encrypted on your
machine (AES-256-GCM, PBKDF2 passphrase); Cloudflare only stores ciphertext and
the page decrypts it in the browser.

One-time setup (from `cloudflare/`):

```
npx wrangler login
npx wrangler pages project create ibkr-portfolio --production-branch main
npx wrangler kv namespace create SNAPSHOTS        # paste the id into wrangler.toml
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # upload token
npx wrangler pages secret put UPLOAD_TOKEN --project-name ibkr-portfolio
./deploy.sh
```

Then add to `.env` and restart the app:

```
CF_SNAPSHOT_URL=https://ibkr-portfolio.pages.dev/api/snapshot
CF_UPLOAD_TOKEN=<the token from above>
SNAPSHOT_PASSPHRASE=<a long passphrase, e.g. 5+ random words>
```

Click **Refresh** locally ("Synced to cloud" appears next to the time), then
open `https://ibkr-portfolio.pages.dev` on your phone and enter the passphrase.
Re-run `./deploy.sh` only when `templates/index.html` changes; data updates
need no redeploy.

## Run

```
pip install -r requirements.txt
python app.py   # serves on http://localhost:5050
```
