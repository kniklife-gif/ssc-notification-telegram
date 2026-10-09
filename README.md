# SSC Notification → Telegram

Monitors the official Staff Selection Commission (https://ssc.gov.in/) Notice Board and sends a Telegram alert for each genuinely new notice. Designed to run on GitHub Actions every 30 minutes, so your PC does not need to be on. The schedule is currently **disabled** in `monitor.yml` (manual runs only) until the SSC source is verified.

## Status

The monitoring engine, state handling, duplicate protection, Telegram delivery and workflow are complete and covered by offline tests.

**The SSC Notice Board listing request is not yet verified.** ssc.gov.in serves a JavaScript app shell, so notices are loaded dynamically, and the real request/response has not been observed. Until `LISTING_URL` and `_extract_records` in `sources/ssc.py` are completed from a real observation (see the marked section in that file), every run fails with a clear "unverified" source error, sends no alerts and leaves state unchanged. Once a manual run succeeds, enable the schedule by uncommenting the `schedule` lines in `.github/workflows/monitor.yml`.

## Setup

1. Create a Telegram bot with [@BotFather](https://t.me/BotFather) and copy the bot token.
2. Get your chat ID (message the bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates`; for a group, add the bot first).
3. In the GitHub repository go to Settings → Secrets and variables → Actions and add:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. Enable GitHub Actions.
5. Run the workflow once manually (Actions → SSC Notification Monitor → Run workflow).
6. The first successful run creates a baseline: existing notices are recorded and **no alerts are sent**.
7. From then on, each new SSC notice produces one Telegram alert.

## How it behaves

- **Official SSC source only.** No third-party search, mirrors or cached copies; document links stay on `ssc.gov.in`.
- **Runs while your PC is off.** GitHub Actions does the polling.
- **State lives in the repository** (`state/seen.json`), committed by the workflow only when it changes.
- **No duplicates.** Each notice has a deterministic `notice_id` (official ID, else document ID, else document URL, else a SHA-256 of title+date+URL); repeats within a scrape and across runs are ignored.
- **No spam on first run.** The first successful scrape is a baseline only.
- **Failures are never treated as "no notices".** An HTTP error, empty or unrecognised response, or an unexpected listing fails the run and leaves state unchanged. If Telegram delivery fails, that notice stays unsent and is retried on the next run.
- If the SSC ID format ever changes so that none of the saved IDs appear in the listing, the run fails instead of re-alerting everything; reset `state/seen.json` to `{"SSC": []}` to re-baseline.
