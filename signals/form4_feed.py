from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date as _date, timedelta
from typing import TYPE_CHECKING, Optional

from signals.edgar_client import EdgarClient, SEC_BASE, SEC_DATA
from signals.form4_parser import InsiderTransaction, parse_form4

if TYPE_CHECKING:
    from db import Database

logger = logging.getLogger("schwab_bot.edgar.feed")

# Official SEC ticker<->CIK map.
TICKER_MAP_URL = f"{SEC_BASE}/files/company_tickers.json"


@dataclass
class Form4Feed:
    client: EdgarClient
    cache_file: str = "./edgar_seen.json"
    # Optional SQLite persistence. When set, the seen-set and parsed
    # transactions live in the DB instead of the JSON cache file.
    db: Optional["Database"] = None
    _seen: set[str] = field(default_factory=set)
    # Insertion-ordered record of seen accessions, used to cap the JSON cache by
    # RECENCY: accession numbers are NOT chronological, so a lexicographic slice
    # would evict recently-seen filings and re-emit them as duplicates.
    _seen_order: list[str] = field(default_factory=list)
    _ticker_to_cik: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if self.db is not None:
            self._seen = self.db.seen_accessions()
            logger.info("Loaded %d seen filings from DB.", len(self._seen))
        else:
            self._load_cache()

    # ---- seen-cache (so a filing is emitted only once) ----

    def _load_cache(self):
        if self.cache_file and os.path.exists(self.cache_file):
            try:
                with open(self.cache_file) as f:
                    loaded = json.load(f)
                # The file is stored newest-last (insertion order); preserve it.
                self._seen_order = [str(a) for a in loaded]
                self._seen = set(self._seen_order)
                logger.info("Loaded %d seen filings from cache.", len(self._seen))
            except Exception:
                logger.warning("Could not read seen-cache %s; starting empty.",
                               self.cache_file)

    def _save_cache(self):
        if not self.cache_file:
            return
        try:
            # Cap by RECENCY: keep the most-recently-seen 5000 in insertion
            # order. (A lexicographic slice of accession numbers is not
            # chronological and would evict recent filings.)
            self._seen_order = self._seen_order[-5000:]
            with open(self.cache_file, "w") as f:
                json.dump(self._seen_order, f)
        except Exception:
            logger.warning("Could not write seen-cache %s.", self.cache_file)

    # ---- ticker -> CIK resolution (official SEC map) ----

    def _ensure_ticker_map(self):
        if self._ticker_to_cik:
            return
        logger.info("Loading SEC ticker->CIK map...")
        data = self.client.get_json(TICKER_MAP_URL)
        # Format: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "..."}, ...}
        for entry in data.values():
            ticker = str(entry["ticker"]).upper()
            cik = str(entry["cik_str"]).zfill(10)
            self._ticker_to_cik[ticker] = cik
        logger.info("Ticker map loaded: %d symbols.", len(self._ticker_to_cik))

    def cik_for(self, ticker: str) -> str | None:
        self._ensure_ticker_map()
        return self._ticker_to_cik.get(ticker.upper())

    def cik_for_person(self, name: str) -> str | None:
        # browse-edgar's getcompany search matches filer entities (people too).
        q = name.strip().replace(" ", "+")
        url = (f"{SEC_BASE}/cgi-bin/browse-edgar?action=getcompany&company={q}"
               f"&type=4&dateb=&owner=include&count=10&output=atom")
        try:
            text = self.client.get_text(url)
        except Exception as e:
            logger.warning("Name lookup failed for %r: %s", name, e)
            return None
        m = re.search(r"CIK=(\d+)", text)
        if not m:
            logger.warning("No CIK found for name %r.", name)
            return None
        return m.group(1).zfill(10)

    # ---- core: pull recent Form 4 filing descriptors for any CIK ----

    def _recent_form4_for_cik(self, cik: str, limit: int,
                              label: str) -> list[dict]:
        cik = str(cik).zfill(10)
        url = f"{SEC_DATA}/submissions/CIK{cik}.json"
        try:
            data = self.client.get_json(url)
        except Exception as e:
            logger.warning("Submissions fetch failed for %s (CIK %s): %s",
                           label, cik, e)
            return []

        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accns = recent.get("accessionNumber", [])
        dates = recent.get("filingDate", [])
        primary = recent.get("primaryDocument", [])

        # form/accessionNumber/filingDate/primaryDocument are PARALLEL arrays.
        # They are normally equal length, but a truncated/paginated or malformed
        # payload can make some shorter — index only within the common length so
        # a mismatch can't raise IndexError and abort the whole poll cycle.
        n = min(len(forms), len(accns), len(dates))
        out = []
        for i in range(n):
            if forms[i] != "4":
                continue
            out.append({
                "accession": accns[i],
                "filing_date": dates[i],
                "primary_doc": primary[i] if i < len(primary) else "",
                "cik": cik,
            })
            if len(out) >= limit:
                break
        return out

    def recent_form4_accessions(self, ticker: str, limit: int = 20) -> list[dict]:
        cik = self.cik_for(ticker)
        if not cik:
            logger.warning("No CIK found for ticker %s.", ticker)
            return []
        return self._recent_form4_for_cik(cik, limit, label=ticker)

    def recent_form4_for_owner(self, owner_cik: str, limit: int = 20) -> list[dict]:
        return self._recent_form4_for_cik(
            owner_cik, limit, label=f"owner CIK {owner_cik}")

    def fetch_transactions(self, descriptor: dict) -> list[InsiderTransaction] | None:
        cik_num = str(int(descriptor["cik"]))  # strip leading zeros for the path
        accn_nodash = descriptor["accession"].replace("-", "")
        base = f"{SEC_BASE}/Archives/edgar/data/{cik_num}/{accn_nodash}"
        primary = descriptor.get("primary_doc", "")

        # Firehose descriptors carry a direct Archives-relative path to the full
        # submission .txt (from the daily index). Use it as the FIRST candidate;
        # it always embeds the <ownershipDocument> block.
        file_name = descriptor.get("file_name", "")

        # Build candidate URLs, preferred first. The submissions API's
        # primaryDocument is usually the XSL-RENDERED HTML under an
        # "xslF345X0N/" subdirectory (e.g. "xslF345X06/primary_doc.xml") — that
        # is HTML, not XML, and feeding it to the XML parser is what produced the
        # old "mismatched tag" warnings. The raw ownership XML lives at the same
        # filename WITHOUT that subdirectory, so strip the prefix and fetch that
        # first; fall back to the full-submission .txt, which always embeds the
        # <ownershipDocument> block.
        candidates = []
        if file_name:
            candidates.append(f"{SEC_BASE}/Archives/{file_name.lstrip('/')}")
        if primary:
            raw = primary.rsplit("/", 1)[-1]      # drop any "xslF345X0N/" prefix
            if not raw.endswith(".xml"):
                raw = raw.rsplit(".", 1)[0] + ".xml"
            candidates.append(f"{base}/{raw}")
        candidates.append(f"{base}/{descriptor['accession']}.txt")
        # De-dup while preserving order.
        _seen_urls: set[str] = set()
        candidates = [u for u in candidates
                      if not (u in _seen_urls or _seen_urls.add(u))]

        got_document = False
        for url in candidates:
            try:
                text = self.client.get_text(url)
            except Exception:
                continue
            # Only the raw ownership XML is parseable. Anything without the
            # <ownershipDocument> root (e.g. the XSL-rendered HTML page, or an
            # EDGAR error page served with a 200) is skipped QUIETLY rather
            # than fed to the XML parser — and crucially does NOT count as a
            # successful retrieval: marking a filing seen on the strength of
            # an HTML page would permanently lose it if the parseable
            # document was merely temporarily unavailable.
            if "<ownershipDocument" not in text:
                continue
            got_document = True  # an actual ownership document was examined
            start = text.find("<ownershipDocument")
            end = text.find("</ownershipDocument>")
            if start != -1 and end != -1:
                xml = text[start:end + len("</ownershipDocument>")]
                txs = parse_form4(xml)
                if txs:
                    return txs
        if got_document:
            # The ownership document was examined and held no transactions we
            # track — a legitimately empty result. Safe to mark the filing seen.
            return []
        # No parseable document could be obtained (network errors, 404s, or
        # only HTML came back). Signal a HARD failure so poll() leaves the
        # accession unseen and retries it, rather than losing the filing.
        logger.warning("Could not fetch any Form 4 document for %s; will retry.",
                       descriptor["accession"])
        return None

    # ---- shared filing processor (used by every poll mode) ----

    def _process_descriptors(self, descriptors: list[dict],
                             label: str) -> list[InsiderTransaction]:
        new_transactions: list[InsiderTransaction] = []
        for d in descriptors:
            accn = d["accession"]
            if accn in self._seen:
                continue
            txs = self.fetch_transactions(d)
            if txs is None:
                # Hard fetch failure — leave unseen so the next poll retries it.
                continue
            recorded_ok = True
            for t in txs:
                new_transactions.append(t)
                if self.db is not None:
                    try:
                        self.db.record_insider_transaction(
                            t, accn, d["filing_date"])
                    except Exception:
                        # Don't mark seen on a DB write failure: the unique
                        # natural-key index makes a retry idempotent (already-
                        # written rows are ignored), so leaving it unseen
                        # recovers the lost rows on the next poll instead of
                        # dropping them permanently.
                        logger.exception(
                            "Could not record insider tx; leaving %s unseen "
                            "to retry.", accn)
                        recorded_ok = False
            if not recorded_ok:
                continue
            self._seen.add(accn)
            self._seen_order.append(accn)
            if self.db is not None:
                self.db.mark_filing_seen(accn)
            if txs:
                logger.info("New Form 4 %s (%s): %d transaction(s).",
                            accn, label, len(txs))
        return new_transactions

    def poll(self, tickers: list[str], per_ticker: int = 10) -> list[InsiderTransaction]:
        new_transactions: list[InsiderTransaction] = []
        for ticker in tickers:
            descriptors = self.recent_form4_accessions(ticker, limit=per_ticker)
            new_transactions.extend(self._process_descriptors(descriptors, ticker))
        if self.db is None:
            self._save_cache()
        return new_transactions

    def poll_owners(self, owners: list[str],
                    per_owner: int = 20) -> list[InsiderTransaction]:
        new_transactions: list[InsiderTransaction] = []
        for who in owners:
            who = who.strip()
            if not who:
                continue
            cik = who if who.isdigit() else self.cik_for_person(who)
            if not cik:
                continue
            label = f"{who} (CIK {str(cik).zfill(10)})" if not who.isdigit() else f"CIK {who}"
            descriptors = self.recent_form4_for_owner(cik, limit=per_owner)
            new_transactions.extend(self._process_descriptors(descriptors, label))
        if self.db is None:
            self._save_cache()
        return new_transactions

    # ---- firehose: scan the daily index for ALL Form 4 filers ----

    def daily_form4_index(self, day: _date) -> list[dict]:
        q = (day.month - 1) // 3 + 1
        url = (f"{SEC_BASE}/Archives/edgar/daily-index/{day.year}/QTR{q}/"
               f"form.{day.strftime('%Y%m%d')}.idx")
        try:
            text = self.client.get_text(url)
        except Exception:
            return []  # weekend/holiday/not-yet-published
        out = []
        for line in text.splitlines():
            # Fixed-ish columns: "4   <company>   <cik>   <YYYYMMDD>   <path>".
            if line[:6].strip() != "4":
                continue
            parts = line.split()
            if len(parts) < 5:
                continue
            file_name = parts[-1]
            date_filed = parts[-2]
            cik = parts[-3]
            # accession = the .txt basename, e.g. 0000950103-26-008011
            accn = file_name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            iso = (f"{date_filed[:4]}-{date_filed[4:6]}-{date_filed[6:8]}"
                   if len(date_filed) == 8 and date_filed.isdigit() else date_filed)
            out.append({
                "accession": accn,
                "filing_date": iso,
                "file_name": file_name,   # direct Archives-relative doc path
                "cik": cik,
            })
        return out

    def poll_firehose(self, days_back: int = 1, today: _date | None = None,
                      max_filings: int | None = None) -> list[InsiderTransaction]:
        today = today or _date.today()
        new_transactions: list[InsiderTransaction] = []
        processed = 0
        for back in range(days_back):
            day = today - timedelta(days=back)
            descriptors = self.daily_form4_index(day)
            if not descriptors:
                continue
            # Drop already-seen up front so max_filings counts only real work.
            fresh = [d for d in descriptors if d["accession"] not in self._seen]
            if max_filings is not None:
                room = max_filings - processed
                if room <= 0:
                    logger.warning("Firehose cap %d reached; %d filing(s) for %s "
                                   "NOT processed.", max_filings,
                                   len(fresh), day)
                    break
                if len(fresh) > room:
                    logger.warning("Firehose cap %d reached; skipping %d of %s's "
                                   "%d filings.", max_filings,
                                   len(fresh) - room, day, len(fresh))
                    fresh = fresh[:room]
            logger.info("Firehose %s: %d new Form 4 filing(s).", day, len(fresh))
            new_transactions.extend(
                self._process_descriptors(fresh, f"firehose {day}"))
            processed += len(fresh)
        if self.db is None:
            self._save_cache()
        return new_transactions
