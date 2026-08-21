# News Terminal Upgrade Ledger

This ledger tracks the 100-upgrade news-terminal plan. Paper is the default. Live execution is opt-in with `python lighter_news_sniper.py --live`. `--live` is the operator confirmation; the kill switch still wins.

## Implemented

### A. Source registry and coverage (1–10)
- Typed `NewsSourceConfig` with adapter, interval, timeout, quota, trust, language, region, category, official domain, live allow.
- `NewsSourceRegistry` with built-in defaults plus `NEWS_EXTRA_SOURCES` JSON.
- Publisher identity preserved (not collapsed to `source="RSS"`).
- RSS/Atom coverage for major crypto publishers, official blogs, and regulators.
- REST/JSON adapter, official announcement adapter, webhook ingest interface (no hardcoded credentials).
- Categories: official, regulator, exchange, media, research, social, unverified.
- Allowlist/denylist via `NEWS_SOURCE_ALLOWLIST` / `NEWS_DISABLED_SOURCES`. Unknown social/unverified sources cannot go live.

### B. Fetching and resilience (11–20)
- Per-source scheduling (due-only polling).
- Bounded concurrency, per-domain rate limiting, timeouts, 2MB size limit.
- Exponential backoff / circuit breakers, HTTP 429 `Retry-After`.
- ETag / Last-Modified conditional requests.
- Persistent source health in SQLite.
- Malformed feeds are quarantined; other sources continue.

### C–D. Normalization, provenance, dedupe, clustering (21–40)
- Provenance-aware `NormalizedNewsEvent` with hashes, latency, classifier version.
- UTC timestamps; naive timestamps rejected.
- Canonical URLs, HTML/Unicode cleanup, byte limits.
- GUID / URL / content-hash dedupe, near-duplicate titles, independent-source copies kept for confirmation.
- Cluster IDs, correction invalidation of pending intents, SQLite persistence.

### E–F. Entities, classification, confirmation (41–60)
- Symbol aliases, contract addresses, chain extraction with ambiguity rejection.
- Official-domain verification.
- Versioned rule registry: listing, delisting, approval, rejection, exploit, outage, partnership, unlock, governance, funding, macro, liquidation, regulatory, rumor, satire, opinion, correction.
- Negation, rumor, contradiction scoring, official-source confirmation shortcuts.

### G–H. Markets, intents, risk, live safety (61–80)
- Asset/market registry with market index, TP/SL, enabled sides.
- Live ticker snapshots from Lighter `orderBookDetails`; stale/spread/volatility vetoes.
- Serialized trade-intent queue, duplicate-event prevention, exposure reservations.
- Shared `LighterNewsRiskGate` for news and live sizing.
- Live collateral uses the configured `LIGHTER_ACCOUNT_INDEX` and fails closed (no `$5.52` fallback).
- Daily loss, consecutive-loss, session-count, cooldown, per-asset and directional caps.
- Telegram authorization required in live mode (never fail-open).
- `--live` enables live execution; `NEWS_KILL_SWITCH=true` disables it.

### I. Order lifecycle and exits (81–90)
- Intent states: intent → reserved → submitted/filled/rejected/invalidated.
- Positions activate from fill quantity/price and persist across restart.
- Exit retry queue with backoff; emergency flatten (`/flatten`).
- Paper fills use live-style slippage/fees; live fills go through SignerClient.

### J. Observability and ops (91–100)
- Structured JSON audit log (`news_audit.jsonl`) with correlation IDs.
- Metrics: ingested, unconfirmed, vetoed, filled_paper, filled_live, corrections.
- Telegram: `/sources`, `/newshealth`, `/signals`, `/rejected`, `/intents`, `/newsmetrics`, `/flatten`.
- Fixture replay without order submission.
- Deterministic tests for adapters, timestamps, duplicates, corrections, kill switch, live enablement, paper fills, restart recon.
- Upgrade ledger and paper-default env examples.

## Live run

```text
python lighter_news_sniper.py --live
```

Required for live fills: `LIGHTER_API_PRIVATE_KEY`, `LIGHTER_ACCOUNT_INDEX`, `WALLET_ADDRESS`, `ADMIN_CHAT_ID`, reachable Lighter API, signer import. Missing any of those fail-closes. `NEWS_KILL_SWITCH=true` blocks live immediately.

## Live-quality pass (funded account)

- Hard veto for insurtech/gaming/DePIN/noise; only listing/delisting/exploit/approval/rejection/outage auto-trade.
- Headline must name the asset; 2 independent sources except SEC/CFTC official approvals.
- Legacy catalyst fallback disabled. Min lot is skipped when it exceeds risk budget (never bumped).
- Live fill is confirmed on-exchange before the book activates; reduce-only TP/SL attached via SDK.
- Daily loss persists in SQLite across restarts. Startup reconcile adopts or flattens exchange positions.
- Shadow mode (`NEWS_PROMOTION_MODE=shadow` or `/shadow`). `/live` requires a confirm tap. `/kill` `/positions` `/flatten`.
- 5-minute Telegram heartbeat. VPS `watchdog.bat` restarts a dead python and rotates `sniper.log`.

## Buy / sell / TP / SL loop

- Per-asset exit policy (`trade_exits.py`): FX 0.4/0.3, index 1.2/0.8, commodity 2.0/1.2, crypto 2.5/1.5, equity 1.5/1.0.
- Live fills wait for exchange size, attach reduce-only GTT TP/SL (`create_tp_limit_order` / `create_sl_limit_order`), then verify those orders on the book.
- Local watchdog is the backup: trailing SL (arm then gap), time-stop, 20% cross-asset price skip.
- Trailing SL amends the exchange SL when it moves. One open position per market. Live spread skip. Partial fills size TP/SL to filled qty.
- Close polls until flat (3 attempts). Flatten uses each asset's own mark, not ETH.
- Reconcile adopts exchange positions and attaches the matching TP/SL.
- Telegram fill/exit cards; `/positions` shows TP/SL/time-stop; manual `buy aapl` / `short gold` / `long spy` go through the same $50 engine.

## Safety rule

No source adapter submits an order. Only a confirmed normalized event that passes the shared risk gate may create a trade intent. Paper remains the default process; live requires `--live`.
