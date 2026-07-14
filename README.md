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

## Run

```
pip install -r requirements.txt
python app.py   # serves on http://localhost:5050
```
