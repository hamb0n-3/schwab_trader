from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # avoid importing schwab/etc. at runtime; keeps db.py light
    from client import AccountSnapshot
    from config import Config
    from backtest import BacktestResult
    from signals.form4_parser import InsiderTransaction
    from signals.congress_parsers import CongressTransaction

logger = logging.getLogger("schwab_bot.db")

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS daily_risk_state (
    date TEXT PRIMARY KEY,              -- 'YYYY-MM-DD' US/Eastern
    starting_equity REAL NOT NULL,
    updated_at TEXT NOT NULL            -- UTC ISO8601
);

CREATE TABLE IF NOT EXISTS seen_filings (
    accession TEXT PRIMARY KEY,
    seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,                 -- BUY / SELL
    order_kind TEXT NOT NULL,          -- BRACKET_ENTRY / MARKET_EXIT
    quantity REAL NOT NULL,
    limit_price REAL,
    take_profit REAL,
    stop_loss REAL,
    live_price REAL,
    notional REAL,
    mode TEXT NOT NULL,                -- DRY_RUN / LIVE
    status TEXT NOT NULL,              -- PREVIEW_OK/PREVIEW_REJECTED/PLACED/BLOCKED/SKIPPED
    broker_order_id TEXT,
    reason TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_symbol_ts ON orders(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);

CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    cash REAL NOT NULL,
    equity REAL NOT NULL,
    buying_power REAL NOT NULL,
    total_exposure REAL NOT NULL,
    num_positions INTEGER NOT NULL,
    positions_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON account_snapshots(ts);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,              -- BUY / EXIT
    reason TEXT,
    holding INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_ts ON signals(symbol, ts);

CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    fast INTEGER,
    slow INTEGER,
    take_profit_pct REAL,
    stop_loss_pct REAL,
    starting_cash REAL NOT NULL,
    ending_equity REAL NOT NULL,
    total_return_pct REAL,
    num_trades INTEGER,
    win_rate REAL,
    profit_factor REAL,
    max_drawdown_pct REAL,
    bars_from TEXT,
    bars_to TEXT,
    equity_curve_json TEXT
);

CREATE TABLE IF NOT EXISTS backtest_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_id INTEGER NOT NULL REFERENCES backtests(id),
    symbol TEXT,
    entry_date TEXT,
    entry_price REAL,
    exit_date TEXT,
    exit_price REAL,
    shares INTEGER,
    reason TEXT,
    pnl REAL,
    return_pct REAL
);
CREATE INDEX IF NOT EXISTS idx_bt_trades_backtest ON backtest_trades(backtest_id);

CREATE TABLE IF NOT EXISTS congress_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                 -- UTC ISO8601 insert time
    accession TEXT NOT NULL,          -- namespaced: house-<docid> / senate-<uuid>
    chamber TEXT NOT NULL,            -- 'house' / 'senate'
    politician TEXT,
    owner TEXT,                       -- '', SP/JT/DC (house) or Self/Spouse/Joint
    ticker TEXT,
    asset_description TEXT,
    asset_type TEXT,                  -- house bracket code 'ST'/'OT'/... or senate 'Stock'/...
    tx_type TEXT,                     -- normalized: P / S / E
    raw_type TEXT,                    -- 'S (partial)', 'Sale (Full)', ...
    transaction_date TEXT,            -- ISO
    notification_date TEXT,           -- ISO ('' for senate)
    filing_date TEXT,                 -- ISO
    amount_min REAL,
    amount_max REAL,                  -- NULL for open-ended 'Over $X'
    amount_raw TEXT,
    doc_id TEXT,
    link TEXT,
    tx_seq INTEGER NOT NULL DEFAULT 0 -- row index within the filing
);
CREATE INDEX IF NOT EXISTS idx_congress_ticker
    ON congress_transactions(ticker, transaction_date);
CREATE INDEX IF NOT EXISTS idx_congress_politician
    ON congress_transactions(politician, transaction_date);
-- Natural key is (accession, tx_seq), NOT the row values: one filing can
-- legitimately contain two identical transaction rows, so a value-based key
-- would silently drop real duplicates. Created in _create_schema like the
-- insider dedup index (tolerant of pre-existing data).

CREATE TABLE IF NOT EXISTS insider_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    accession TEXT NOT NULL,
    filing_date TEXT,
    issuer_name TEXT,
    ticker TEXT,
    issuer_cik TEXT,
    owner_name TEXT,
    owner_cik TEXT,
    is_director INTEGER,
    is_officer INTEGER,
    officer_title TEXT,
    security_title TEXT,
    transaction_code TEXT,
    transaction_date TEXT,
    shares REAL,
    price_per_share REAL,
    acquired_disposed TEXT,
    is_derivative INTEGER,
    dollar_value REAL
);
CREATE INDEX IF NOT EXISTS idx_insider_ticker
    ON insider_transactions(ticker, transaction_date);
-- NOTE: the idx_insider_owner index references owner_cik and is created in
-- _create_schema() AFTER the additive ALTER (an old DB won't have the column
-- yet, and CREATE INDEX here would fail before the migration runs).
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._closed = False
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # Wait briefly for a competing writer (e.g. the standalone insider
        # runner, or a backtest, writing to the same DB) instead of raising
        # "database is locked" immediately. WAL already lets readers run
        # concurrently with a writer; this covers writer-vs-writer races.
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._create_schema()
        logger.info("SQLite persistence ready at %s", path)

    def _create_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # Additive migration: a DB created before owner_cik existed won't have
            # the column (CREATE TABLE IF NOT EXISTS won't add it). Add it on the
            # fly so old databases keep working. (Index in _SCHEMA is created
            # after; it tolerates the column being freshly added.)
            cols = {r["name"] for r in
                    self._conn.execute("PRAGMA table_info(insider_transactions)")}
            if "owner_cik" not in cols:
                self._conn.execute(
                    "ALTER TABLE insider_transactions ADD COLUMN owner_cik TEXT")
            # Now that owner_cik is guaranteed present (fresh or migrated),
            # create its index.
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_insider_owner "
                "ON insider_transactions(owner_cik, transaction_date)")
            # Dedup guard for insider rows: a UNIQUE index on the natural key so
            # re-emitting a filing (e.g. a retry after a mid-filing failure)
            # can't insert duplicate transactions. Created tolerantly — a
            # pre-existing DB that already holds duplicates would reject the
            # index, so log and continue rather than fail startup.
            try:
                self._conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_insider_natkey "
                    "ON insider_transactions(accession, owner_name, security_title, "
                    "transaction_code, transaction_date, shares, price_per_share, "
                    "acquired_disposed, is_derivative)"
                )
            except sqlite3.OperationalError:
                logger.warning(
                    "Could not create insider dedup index (pre-existing "
                    "duplicates?); duplicate insider rows won't be blocked.")
            # Congress dedup: (accession, tx_seq) makes a mid-filing retry's
            # re-inserts idempotent without dropping a filing's legitimately
            # duplicated rows. Same tolerant creation as the insider index.
            try:
                self._conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_congress_natkey "
                    "ON congress_transactions(accession, tx_seq)"
                )
            except sqlite3.OperationalError:
                logger.warning(
                    "Could not create congress dedup index (pre-existing "
                    "duplicates?); duplicate congress rows won't be blocked.")
            row = self._conn.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            self._conn.commit()

    # ---------------------------------------------------------------- writes

    def record_order(
        self,
        symbol: str,
        side: str,
        order_kind: str,
        quantity: float,
        *,
        mode: str,
        status: str,
        limit_price: float | None = None,
        take_profit: float | None = None,
        stop_loss: float | None = None,
        live_price: float | None = None,
        notional: float | None = None,
        broker_order_id: str | None = None,
        reason: str | None = None,
        detail: str | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO orders
                   (ts, symbol, side, order_kind, quantity, limit_price,
                    take_profit, stop_loss, live_price, notional, mode, status,
                    broker_order_id, reason, detail)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_utc_now(), symbol, side, order_kind, quantity, limit_price,
                 take_profit, stop_loss, live_price, notional, mode, status,
                 broker_order_id, reason, detail),
            )
            self._conn.commit()
            return cur.lastrowid

    def record_snapshot(self, snap: "AccountSnapshot") -> None:
        positions = [
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "average_price": p.average_price,
                "market_value": p.market_value,
            }
            for p in snap.positions
        ]
        with self._lock:
            self._conn.execute(
                """INSERT INTO account_snapshots
                   (ts, cash, equity, buying_power, total_exposure,
                    num_positions, positions_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (_utc_now(), snap.cash, snap.equity, snap.buying_power,
                 snap.total_exposure, len(snap.positions), json.dumps(positions)),
            )
            self._conn.commit()

    def record_signal(self, symbol: str, action: str, reason: str,
                      holding: bool) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO signals (ts, symbol, action, reason, holding) "
                "VALUES (?,?,?,?,?)",
                (_utc_now(), symbol, action, reason, 1 if holding else 0),
            )
            self._conn.commit()

    # ---- daily risk state (replaces daily_state.json) ----

    def get_daily_state(self, date: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT date, starting_equity FROM daily_risk_state WHERE date = ?",
                (date,),
            ).fetchone()
        if row is None:
            return None
        return {"date": row["date"], "starting_equity": row["starting_equity"]}

    def set_daily_state(self, date: str, starting_equity: float) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO daily_risk_state (date, starting_equity, updated_at)
                   VALUES (?,?,?)
                   ON CONFLICT(date) DO UPDATE SET
                       starting_equity = excluded.starting_equity,
                       updated_at = excluded.updated_at""",
                (date, starting_equity, _utc_now()),
            )
            self._conn.commit()

    # ---- seen Form 4 filings (replaces edgar_seen.json) ----

    def seen_accessions(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT accession FROM seen_filings").fetchall()
        return {r["accession"] for r in rows}

    def is_filing_seen(self, accession: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM seen_filings WHERE accession = ?", (accession,)
            ).fetchone()
        return row is not None

    def mark_filing_seen(self, accession: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO seen_filings (accession, seen_at) VALUES (?,?)",
                (accession, _utc_now()),
            )
            self._conn.commit()

    def record_insider_transaction(self, tx: "InsiderTransaction",
                                   accession: str, filing_date: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO insider_transactions
                   (ts, accession, filing_date, issuer_name, ticker, issuer_cik,
                    owner_name, owner_cik, is_director, is_officer, officer_title,
                    security_title, transaction_code, transaction_date, shares,
                    price_per_share, acquired_disposed, is_derivative, dollar_value)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_utc_now(), accession, filing_date, tx.issuer_name, tx.ticker,
                 tx.issuer_cik, tx.owner_name, getattr(tx, "owner_cik", ""),
                 int(tx.is_director), int(tx.is_officer), tx.officer_title,
                 tx.security_title, tx.transaction_code, tx.transaction_date,
                 tx.shares, tx.price_per_share, tx.acquired_disposed,
                 int(tx.is_derivative), tx.dollar_value),
            )
            self._conn.commit()

    def record_congress_transaction(self, tx: "CongressTransaction") -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO congress_transactions
                   (ts, accession, chamber, politician, owner, ticker,
                    asset_description, asset_type, tx_type, raw_type,
                    transaction_date, notification_date, filing_date,
                    amount_min, amount_max, amount_raw, doc_id, link, tx_seq)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_utc_now(), tx.accession, tx.chamber, tx.politician, tx.owner,
                 tx.ticker, tx.asset_description, tx.asset_type, tx.tx_type,
                 tx.raw_type, tx.transaction_date, tx.notification_date,
                 tx.filing_date, tx.amount_min, tx.amount_max, tx.amount_raw,
                 tx.doc_id, tx.link, tx.tx_seq),
            )
            self._conn.commit()

    def import_seen_json(self, path: str) -> None:
        if not path or not os.path.exists(path):
            return
        try:
            with open(path) as f:
                accns = json.load(f)
            if not isinstance(accns, list):
                raise ValueError("seen-cache is not a JSON list")
            with self._lock:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO seen_filings (accession, seen_at) "
                    "VALUES (?,?)",
                    [(str(a), _utc_now()) for a in accns],
                )
                self._conn.commit()
            logger.info("Imported %d seen entries from %s.", len(accns), path)
        except Exception:
            logger.warning("Could not import seen-cache %s.", path)

    def record_backtest(
        self,
        result: "BacktestResult",
        *,
        fast: int | None = None,
        slow: int | None = None,
        take_profit_pct: float | None = None,
        stop_loss_pct: float | None = None,
        bars_from: str | None = None,
        bars_to: str | None = None,
    ) -> int:
        curve = json.dumps([[d, eq] for d, eq in result.equity_curve])
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO backtests
                   (ts, symbol, fast, slow, take_profit_pct, stop_loss_pct,
                    starting_cash, ending_equity, total_return_pct, num_trades,
                    win_rate, profit_factor, max_drawdown_pct, bars_from, bars_to,
                    equity_curve_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_utc_now(), result.symbol, fast, slow, take_profit_pct,
                 stop_loss_pct, result.starting_cash, result.ending_equity,
                 result.total_return_pct, result.num_trades, result.win_rate,
                 result.profit_factor, result.max_drawdown_pct, bars_from,
                 bars_to, curve),
            )
            backtest_id = cur.lastrowid
            self._conn.executemany(
                """INSERT INTO backtest_trades
                   (backtest_id, symbol, entry_date, entry_price, exit_date,
                    exit_price, shares, reason, pnl, return_pct)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [
                    (backtest_id, t.symbol, t.entry_date, t.entry_price,
                     t.exit_date, t.exit_price, t.shares, t.reason, t.pnl,
                     t.return_pct)
                    for t in result.trades
                ],
            )
            self._conn.commit()
            return backtest_id

    # ---------------------------------------------------------------- migration

    def import_legacy(self, config: "Config",
                      edgar_cache_path: str | None = None) -> None:
        daily_path = getattr(config, "daily_state_file", "") or ""
        base_dir = os.path.dirname(daily_path) or "."
        if edgar_cache_path and os.path.exists(edgar_cache_path):
            edgar_path = edgar_cache_path
        else:
            edgar_path = os.path.join(base_dir, "edgar_seen.json")
            # Form4Feed defaults its cache to ./edgar_seen.json (cwd), which may
            # differ from daily_state_file's directory. Fall back to the cwd
            # default so migration still finds it when configured separately.
            if not os.path.exists(edgar_path):
                edgar_path = os.path.join(".", "edgar_seen.json")

        # daily_state.json -> daily_risk_state
        if daily_path and os.path.exists(daily_path) and self._table_empty(
            "daily_risk_state"
        ):
            try:
                with open(daily_path) as f:
                    data = json.load(f)
                if data.get("date") and data.get("starting_equity") is not None:
                    self.set_daily_state(data["date"], float(data["starting_equity"]))
                    logger.info("Imported legacy daily state from %s", daily_path)
            except Exception:
                logger.warning("Could not import legacy daily state %s.", daily_path)

        # edgar_seen.json -> seen_filings. Deliberately UNGUARDED (no
        # table-empty check): seen_filings is now shared with the congress
        # feed's namespaced accessions, so "table is empty" no longer means
        # "edgar not yet imported" — a congress run first would permanently
        # block this import and re-emit every previously-seen EDGAR filing.
        # import_seen_json's INSERT OR IGNORE makes re-running it a no-op.
        self.import_seen_json(edgar_path)

    def _table_empty(self, table: str) -> bool:
        with self._lock:
            row = self._conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        return row is None

    # ---------------------------------------------------------------- reads

    def recent_orders(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def recent_signals(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def recent_insider_buys(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                """SELECT * FROM insider_transactions
                   WHERE transaction_code = 'P' AND acquired_disposed = 'A'
                   ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()

    def recent_insider_transactions(self, limit: int = 50, *,
                                    owner_cik: str | None = None,
                                    ticker: str | None = None,
                                    code: str | None = None) -> list[sqlite3.Row]:
        clauses, params = [], []
        if owner_cik:
            clauses.append("owner_cik = ?"); params.append(owner_cik)
        if ticker:
            clauses.append("ticker = ?"); params.append(ticker.upper())
        if code:
            clauses.append("transaction_code = ?"); params.append(code.upper())
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._lock:
            return self._conn.execute(
                f"SELECT * FROM insider_transactions {where} "
                f"ORDER BY id DESC LIMIT ?", params,
            ).fetchall()

    def recent_congress_transactions(self, limit: int = 50, *,
                                     chamber: str | None = None,
                                     politician: str | None = None,
                                     ticker: str | None = None,
                                     tx_type: str | None = None) -> list[sqlite3.Row]:
        clauses, params = [], []
        if chamber:
            clauses.append("chamber = ?"); params.append(chamber.lower())
        if politician:
            clauses.append("politician LIKE ?"); params.append(f"%{politician}%")
        if ticker:
            clauses.append("ticker = ?"); params.append(ticker.upper())
        if tx_type:
            clauses.append("tx_type = ?"); params.append(tx_type.upper())
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._lock:
            return self._conn.execute(
                f"SELECT * FROM congress_transactions {where} "
                f"ORDER BY id DESC LIMIT ?", params,
            ).fetchall()

    def recent_congress_buys(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                """SELECT * FROM congress_transactions
                   WHERE tx_type = 'P' ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()

    def recent_backtests(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM backtests ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def equity_curve(self, limit: int | None = None) -> list[sqlite3.Row]:
        with self._lock:
            if limit:
                rows = self._conn.execute(
                    "SELECT ts, equity, cash FROM account_snapshots "
                    "ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return list(reversed(rows))
            return self._conn.execute(
                "SELECT ts, equity, cash FROM account_snapshots ORDER BY id"
            ).fetchall()

    def latest_snapshot(self) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM account_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()

    def order_status_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM orders GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()
