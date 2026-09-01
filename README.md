# Schwab Trader API Bot

A comprehensive, safety-first automated trading bot for the **Charles Schwab Trader API**, written in Python on top of [`schwab-py`](https://schwab-py.readthedocs.io). It fetches market data, runs a pluggable strategy, and places **bracketed orders** (entry + take-profit + stop-loss) with hard risk limits and a kill switch.

> **This trades real money.** There is no Schwab paper-trading endpoint. The bot ships in a safe **dry-run** mode that validates orders without placing them, and requires a deliberate two-step opt-in before it will ever submit a real order. Read the whole README before going live. This is educational software provided as-is, with no warranty; you are responsible for every trade it makes.

---

## What it does

- **Market data** — REST quotes + daily price history, plus an optional real-time WebSocket streamer.
- **Strategy** — a clean `Strategy` interface with an example SMA-crossover (swap in your own).
- **Bracket orders** — every entry places a take-profit limit and a stop-loss stop as a linked OCO that fires once the entry fills (verified TRIGGEROCO structure).
- **Risk layer** — per-order $ cap, per-symbol share cap, total exposure cap, max open positions, daily-loss halt, fat-finger price check, per-symbol cooldown, and a per-symbol daily entry cap (an SMA cross stays true all day; without the cap a stop-out would re-enter every cooldown window).
- **Kill switch** — drop a file (or run `python run.py panic`) and the bot flattens everything and stops.
- **Dry-run by default** — uses Schwab's `preview_order` to validate orders and logs exactly what it *would* do.

## File layout

| File | Purpose |
|------|---------|
| `config.py` | All settings + safety limits, loaded from `.env` |
| `auth.py` | OAuth login + token lifecycle + account-hash lookup |
| `client.py` | Schwab API wrapper: quotes, account, positions, order builders |
| `risk.py` | The safety layer — every order passes through here |
| `strategy.py` | Strategy interface + example SMA crossover |
| `market_calendar.py` | Holiday- and half-day-aware US market hours (NYSE) |
| `backtest.py` | Backtesting harness — reuses the live Strategy interface |
| `executor.py` | Sizes positions, builds brackets, places/previews orders |
| `streaming.py` | Optional real-time quote streaming (also a data-entitlement test) |
| `notify.py` | Push notifications: Signal webhook (info) + Pushover (urgent) |
| `bot.py` | Main loop: snapshot signal route, with market-hours + shutdown |
| `run.py` | CLI: `check`, `quote`, `preview`, `run`, `panic`, `resume` |
| `signals/edgar_client.py` | SEC-compliant EDGAR HTTP client (rate limit + User-Agent) |
| `signals/form4_parser.py` | Parses Form 4 XML into structured insider transactions |
| `signals/form4_feed.py` | Polls EDGAR for new Form 4 filings on a watchlist |
| `signals/insider_strategy.py` | Filters insider buys into `Signal`s (the bot's interface) |
| `signals/run_insider.py` | Standalone CLI for the insider feed |
| `signals/congress_client.py` | Polite client for House Clerk + Senate eFD (session handshake) |
| `signals/congress_parsers.py` | Parses House PTR PDFs & Senate PTR HTML into transactions |
| `signals/congress_feed.py` | Polls official STOCK Act sources for new PTR filings |
| `signals/congress_strategy.py` | Price-proximity gate: BUY only if the stock hasn't run yet |
| `signals/run_congress.py` | Standalone CLI for the congressional feed |

---

## Setup

### 1. Register a Schwab developer app
1. Go to **developer.schwab.com**, create an account, and create an app.
2. Add both API products: **Accounts and Trading Production** and **Market Data Production**.
3. Set the **callback URL** to exactly `https://127.0.0.1:8182` (must match `.env`).
4. Wait for the app status to move from *Approved – Pending* to **Ready For Use** (typically 1–3 business days).
5. Copy your **App Key** and **Secret**.

### 2. Install
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure
```bash
cp .env.example .env
# edit .env: paste your App Key/Secret, set SYMBOLS, sizing, and limits
```

### 4. First login (creates the token)
```bash
python run.py check
```
A browser opens for the Schwab login + consent flow. After you approve, a token file is written and the command prints your account balances and positions.

> **The refresh token expires every 7 days.** When it does, re-run any command and log in again. A good habit: re-run `python run.py check` every **Sunday** before the market opens so the 7-day clock never expires mid-week.

---

## Verify before trading

```bash
python run.py quote AAPL # is your data real-time or ~15 min delayed?
python streaming.py # live streaming quotes (Ctrl+C to stop)
python run.py preview # build a sample bracket and validate via Schwab (no order)
```

If quotes look ~15 minutes stale, accept the **market-data agreements** in your Schwab account settings — real-time access is entitlement-based.

---

## Running

### Dry-run (default, safe)
```bash
python run.py run
```
The bot logs every signal and every order it *would* place, validated against Schwab via `preview_order`. **No real orders are submitted.** Run it this way for at least a couple of weeks and read the logs.

### Going live (real money) — deliberate two-step opt-in
Only after you've watched dry-run behave correctly:
1. In `.env`, set `DRY_RUN=false`
2. In `.env`, set `LIVE_CONFIRM=I_UNDERSTAND_THIS_TRADES_REAL_MONEY`

Both are required. If either is missing, the bot runs but stays in safe mode and tells you so. Start with the **smallest position size you can** and money you can afford to lose entirely.

Config is read **once at startup** — editing `.env` while the bot runs does
nothing until you restart it.

### What dry-run does NOT rehearse

Dry-run validates orders via `preview_order` but never places them, so the
broker never holds bot-created orders or positions. That means several
live-only behaviors are **not exercised** during a dry-run soak, and you may
see them for the first time after flipping live:

- **The stacking guard and in-flight caps never fire.** With no working
 orders at the broker, the "entry already working — skip" path and the
 slow-fill exposure/position accounting only activate live. Their effect is
 always *more* conservative (blocking entries), never less.
- **The live exit sequence never runs.** Cancel resting brackets re-check
 the position market sell, including the abort-on-failed-cancel and the
 `POSITION UNPROTECTED` page when the sell itself fails — all live-only.
- **Flatten-all's cancel pass is live-only.** A dry-run kill-switch test
 previews market sells of whatever REAL positions the account holds (it
 places nothing), but does not rehearse the cancel-everything-first step.
- **The daily-loss halt measures your real account equity**, including
 positions the bot didn't open. Note it re-baselines to current equity at
 each new ET day, so overnight/weekend gap losses never count toward it.

### Panic
```bash
python run.py panic # engage kill switch -> bot flattens all & stops next cycle
python run.py resume # clear the kill switch
```

### Notifications (optional)

Two severity-routed channels, configured in `.env`. Leave either unset to disable it; failures never interfere with trading.

**Signal messenger — informational** (strategy signals, order outcomes, start/stop). Point it at a webhook you host, e.g. [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api)'s `/v2/send`:
```ini
SIGNAL_WEBHOOK_URL=http://localhost:8080/v2/send
SIGNAL_NUMBER=+15551234567 # sender registered with signal-cli
SIGNAL_RECIPIENTS=+15557654321 # comma-separated numbers or group ids
```
With `SIGNAL_NUMBER`/`SIGNAL_RECIPIENTS` empty, the POST body is just `{"message": "..."}` — suitable for any generic relay webhook.

**Pushover — urgent/critical** (daily-loss halt, kill switch, flatten-all, an exit aborted because resting orders wouldn't cancel). Urgent messages are also mirrored to the Signal feed:
```ini
PUSHOVER_TOKEN=your-app-token # from pushover.net
PUSHOVER_USER=your-user-key
PUSHOVER_PRIORITY=1 # -2..2; 2 = emergency (retry until acked)
```
`NOTIFY_COOLDOWN_SECONDS` (default 300) suppresses an identical message repeating within the window.

---

## How an entry works

1. Strategy emits a `BUY` for a symbol.
2. Executor sizes the position from `POSITION_SIZE_USD` and the live price.
3. RiskManager checks all limits; if any fail, the entry is logged and skipped.
4. A bracket is built: BUY limit entry on fill OCO( SELL limit take-profit, SELL stop stop-loss ).
5. Dry-run previews it; live places it and records the order id.

The take-profit and stop-loss are **GTC** and live at the broker, so they protect the position even if the bot process dies.

---

## Backtesting (do this before dry-run)

The backtester runs the **same `Strategy` object** the live bot trades, with the
same bracket exits and realistic costs (commission + slippage), so what you test
is what you trade.

```bash
# From Schwab history (uses your configured credentials):
python backtest.py --symbol AAPL --years 5 --fast 20 --slow 50 --tp 0.05 --sl 0.03

# From a CSV (date,close[,open,high,low]) — no API needed:
python backtest.py --csv mydata.csv --symbol AAPL
```

It prints trades, win rate, total return, profit factor, max drawdown, and — most
importantly — a **buy-and-hold benchmark**. A strategy that "wins" but lags simply
holding the stock is not worth trading.

**Read the limitations in the header of `backtest.py`.** It's a daily-bar
simulator: signals fill at the **next bar's open** (never at the close that
produced them), bars that **gap through a stop fill at the open** (a gapped
stop does not fill at the stop price), it assumes the stop fills before the
target when both are touched in one bar (pessimistic), models costs only
approximately, and does nothing to prevent overfitting. A backtest can
disprove a bad strategy; it cannot prove a good one. Reserve out-of-sample
data, and expect live results to be worse.

## Insider feed (SEC Form 4, official EDGAR only)

An optional data feed that surfaces insider activity from SEC Form 4 filings,
pulled only from official SEC endpoints (`www.sec.gov` / `data.sec.gov`). It's in
the `signals/` package and emits the same `Signal` objects the bot understands, so
insider activity can feed (or filter) entries through the existing executor + risk
+ dry-run layers.

**Four modes** — track by stock, by person, or scan the whole market:

```bash
export EDGAR_USER_AGENT="MyBot/1.0 (you@example.com)" # SEC requires this

# 1. by TICKER — who trades these stocks (the original mode)
python -m signals.run_insider --tickers AAPL,MSFT,NVDA --min-value 250000 --cluster 2
# surface sells (EXIT) + option exercises (INFO) too, not just buys:
python -m signals.run_insider --tickers AAPL --codes P,S,M --include-derivative

# 2. by PERSON (name) — everything they file, across ALL companies
python -m signals.run_insider --insiders "Tim Cook" --codes P,S --group-by insider

# 3. by PERSON (exact CIK) — names aren't unique; CIK is the stable id
python -m signals.run_insider --ciks 0001214156 --codes P,S --group-by insider

# 4. FIREHOSE — discover ANY notable insider market-wide (cap the crawl!)
python -m signals.run_insider --firehose --days-back 1 --max-filings 300 \
 --codes P --min-value 1000000 --group-by insider
```

> Person-mode catches things ticker-mode structurally can't — e.g. Apple's CEO
> buying **Nike** (he's on Nike's board) shows up under `--ciks` for Tim Cook but
> never under `--tickers AAPL`.

What it does (and deliberately doesn't):
- Pulls Form 4 **XML** (structured, no OCR) via the submissions API. Tickers
 resolve through `company_tickers.json`; people resolve by CIK (or name via the
 EDGAR company-search). The **firehose** reads the daily index — every Form 4
 filed that day (often 1500-2000), so it's gated by `--max-filings` (default 300;
 the run logs exactly how many it skipped — no silent truncation).
- **By default** surfaces only **open-market purchases** (code `P`) as BUY
 signals; sells/exercises/grants are off (insiders sell for many non-signal
 reasons). Opt others in with `--codes` (PBUY, SEXIT, M/A/G/FINFO) and
 `--include-derivative`.
- **Capture vs. surface:** *every* parsed transaction (all codes, derivatives
 included) is always written to the SQLite `insider_transactions` table
 (incl. `owner_cik`). Filters only decide which become printed/acted-on
 **signals** — query the DB (or `run.py history`) for the full picture.
- `--group-by insider` reports "PERSON did $X across {tickers}" instead of the
 per-ticker "N insiders did $X of TICKER".
- Sizes positions by **your** `POSITION_SIZE_USD`, not the insider's amount.
- A "seen" cache (`edgar_seen.json`, or the DB when enabled) emits each filing once.

Knobs (`signals/run_insider.py`): mode (`--tickers` / `--insiders` / `--ciks` /
`--firehose`), `--group-by` (`ticker`|`insider`), `--codes` (default `P`),
`--include-derivative`, `--min-value` (default 100k), `--cluster`,
`--max-age-days` (default 14), `--all-roles`, `--days-back` & `--max-filings`
(firehose).

**Honest caveat — this is a weak, slow signal.** Evidence supports *clusters* of
insider buying mildly predicting returns over **months**, and the EDGAR feed is
watched by everyone, so it's heavily arbitraged. Use it as one input, not a money
printer. The `--cluster N` and `--max-age-days` filters exist precisely so you can
require stronger, fresher signals. SEC rules are respected: a contact User-Agent
is mandatory and requests are rate-limited well under 10/sec.

> Congressional STOCK Act mirroring was intentionally **not** built: those
> disclosures are PDF/OCR, report only dollar *ranges*, and carry a 30–45 day
> reporting lag that destroys the signal. The Form 4 feed is the genuinely
> useful half.



Implement the `Strategy` protocol in `strategy.py`:

```python
@dataclass
class MyStrategy:
 def evaluate(self, symbol, closes, holding) -> Signal:
 # closes: list[float] oldest->newest ; holding: bool
 ...
 return Signal(symbol, Action.BUY, "my reason")
```
Then wire it up in `bot.py` (`self.strategy = MyStrategy(...)`).

---

## Congressional feed (STOCK Act PTRs, official sources only)

Members of Congress must disclose stock trades within 30–45 days (STOCK Act
Periodic Transaction Reports). This pipeline polls the **official sources
directly** — no third-party API:

- **House**: the Clerk's yearly disclosure index ZIP + each PTR's PDF
 (digitally-filed PTRs parse cleanly; scanned/paper filings are skipped with
 a warning). PDF parsing needs `pypdf` — included in `requirements.txt`.
- **Senate**: the eFD search endpoint (the agree-to-terms session handshake is
 handled automatically); electronic PTRs are parsed from HTML, paper ones
 skipped.

**The core signal — "hasn't made a run yet."** Disclosures lag trades by
weeks and contain no price (only ranges like `$15,001 - $50,000`), so most
disclosed buys have already moved by the time you see them. The strategy
proxies the politician's entry with the stock's **close on the transaction
date** and compares it to **today's price** (your Schwab market data):

- still within `--proximity` (default 5%) of that close **BUY** signal —
 the stock hasn't run since the purchase;
- already ran (or fell) beyond the threshold **INFO** — surfaced, but
 you'd be chasing (or catching a knife).

```bash
python -m signals.run_congress # both chambers, last 30 days
python -m signals.run_congress --politicians "Pelosi" --days-back 60
python -m signals.run_congress --chamber senate --min-amount 50000 --cluster 2
python -m signals.run_congress --no-prices --no-db # quick look, no Schwab auth
```

Knobs: `--chamber`, `--days-back`, `--max-filings` (per-run cap on new
filings fetched, default 300; the rest resume next run), `--politicians`
(name substring), `--tickers`, `--min-amount` (vs the range's lower bound,
default 15k), `--max-age-days` (default 60), `--proximity` (default 0.05),
`--cluster`, `--include-sells` (sells are INFO-only — taxes/diversification
make them weak), `--all-assets`, `--no-prices`, `--no-db`, `--cache`,
`--rate`. The yearly House index ZIP is cached on disk with conditional GETs
(`house_*FD.zip` beside the seen-cache), so repeat polls skip the multi-MB
download when nothing changed.

Everything parsed is recorded to the `congress_transactions` table
(`run.py history` shows recent purchases). Without Schwab credentials (or with
`--no-prices`) signals degrade to INFO — the proximity gate needs price data.
Amendments are re-filed under new document ids and will appear again; that is
intentional. Like the insider feed: **informational only**, nothing is traded
automatically, and mirroring politicians is a slow, widely-watched, heavily
arbitraged signal — backtest with the disclosure lag before trusting it.

---

## Important warnings

- **Backtest first.** The included SMA crossover is a *template*, not a profitable strategy. Validate any real strategy on historical data (Backtrader, vectorbt) before risking capital.
- **Holiday & half-day aware.** Market hours use `pandas_market_calendars` (NYSE), so weekends, holidays, and early-close half-days (e.g. the day after Thanksgiving, July 3) are all respected — the bot reads each day's actual close time rather than assuming 16:00. If the library isn't installed, it falls back to a naive weekday + 09:30–16:00 ET check and warns loudly that holidays are not respected.
- **OCO race condition.** Schwab notes there's no guarantee the second OCO leg cancels before it also executes in fast markets. The bot reconciles positions each cycle; don't assume perfect fills.
- **`preview_order` support is uneven.** Dry-run validates orders via Schwab's preview endpoint, but Schwab's preview support for conditional/bracket orders is spotty — a "preview ok=False" in dry-run may mean "preview doesn't support this order shape," not "your order is malformed." Dry-run remains useful as an intent logger regardless.
- **Headless/cloud auth.** The initial OAuth login opens a browser, so you can't do the *first* authentication on a headless server. Authenticate locally, then copy `schwab_token.json` to the server — and re-copy it whenever the 7-day refresh token expires. If a login would be needed and there's no interactive terminal, the bot **fails fast with a clear error** instead of hanging on a browser prompt nobody can see (a hung bot looks exactly like a healthy idle one).
- **Pattern Day Trader rule.** FINRA's $25k PDT minimum is being eliminated effective **June 4, 2026**, phased in by brokers through Oct 2027 — until your broker implements it, the old rule may still apply. The default config uses GTC bracket exits (not intraday round-trips), but if you adapt it to day-trade, track your day-trade count.
- **Secrets.** Never commit `.env`, `schwab_token.json`, or `daily_state.json`. They're in `.gitignore`. Restrict permissions (`chmod 600 .env schwab_token.json`).
- **Taxes.** Frequent trading creates short-term gains and potential wash sales — keep records and consult a tax professional.

## Safety behaviors worth knowing

- **No duplicate entries.** Before placing an entry, the bot checks for any working order already touching that symbol (including unfilled entry limits and resting bracket exits) and skips if one exists — so a slow-to-fill limit can't be re-ordered and stacked. If it can't confirm order state, it fails safe and skips.
- **Symmetric overnight protection.** Both bracket exits (take-profit and stop-loss) are GTC, so a held position keeps both its profit target and its stop overnight, as a matched pair. The entry stays DAY so an unfilled entry expires rather than firing days later on a stale signal.
- **Restart-proof daily-loss halt.** The day's equity baseline is keyed to the ET calendar day and persisted to `daily_state.json`. A mid-day crash/restart reloads the original baseline instead of resetting to a lower (drawn-down) value, so the daily-loss limit stays armed. The baseline rolls over automatically at a new trading day.

---

## Recommended path

1. **Backtest** — `python backtest.py --symbol AAPL --years 5`. Confirm the strategy beats buy-and-hold net of costs on out-of-sample data before going further.
2. `check` / `quote` / `preview` — confirm auth, data, and order validation work.
3. **Dry-run for weeks** — read the logs, confirm signals and sizing look sane.
4. Go live with **tiny** size and all risk limits tight.
5. Only then loosen limits, and only as far as your tested edge justifies.

## TODO
- Optionally wire the insider/congress feeds into bot.py as live signal
 sources (both are deliberately informational-only today; any wiring must go
 through the dry-run + risk layer)
- Lag-aware backtest for the disclosure feeds (insider + congressional)
- Multi-symbol portfolio backtesting (testing your whole SYMBOLS list at once with shared capital)
- A walk-forward harness that splits data into in-sample/out-of-sample windows automatically to make overfitting harder
- DB retention policy (account_snapshots grows ~100k rows/yr — harmless to
 SQLite, but unbounded)