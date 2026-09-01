from __future__ import annotations

import io
import json
import logging
import os
import zipfile
from dataclasses import dataclass, field
from datetime import date as _date, timedelta
from typing import TYPE_CHECKING, Callable, Optional

from signals.congress_client import CongressClient, HOUSE_BASE
from signals.congress_parsers import (
    CongressTransaction,
    extract_senate_row,
    parse_house_index_xml,
    parse_house_ptr_text,
    parse_senate_ptr_html,
)

if TYPE_CHECKING:
    from db import Database

try:
    from pypdf import PdfReader
    _HAVE_PYPDF = True
except ImportError:  # pragma: no cover
    _HAVE_PYPDF = False

logger = logging.getLogger("schwab_bot.congress.feed")

_SENATE_PAGE_SIZE = 100
# Sanity ceiling on the uncompressed yearly House FD index (the real file is a
# few MB). Above this we assume a corrupt/hostile ZIP and skip it.
_MAX_INDEX_BYTES = 256 * 1024 * 1024


@dataclass
class CongressFeed:
    client: CongressClient
    cache_file: str = "./congress_seen.json"
    db: Optional["Database"] = None
    _seen: set[str] = field(default_factory=set)
    # Insertion-ordered, to cap the JSON cache by RECENCY (accessions are not
    # chronological; a lexicographic slice would evict recent filings).
    _seen_order: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.db is not None:
            self._seen = self.db.seen_accessions()
            logger.info("Loaded %d seen filings from DB.", len(self._seen))
        else:
            self._load_cache()

    # ---- seen-cache (same shape as Form4Feed) ----

    def _load_cache(self):
        if self.cache_file and os.path.exists(self.cache_file):
            try:
                with open(self.cache_file) as f:
                    loaded = json.load(f)
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
            self._seen_order = self._seen_order[-5000:]
            with open(self.cache_file, "w") as f:
                json.dump(self._seen_order, f)
        except Exception:
            logger.warning("Could not write seen-cache %s.", self.cache_file)

    # ---------------- House ----------------

    def house_ptr_descriptors(self, days_back: int,
                              today: _date | None = None) -> list[dict]:
        today = today or _date.today()
        cutoff = today - timedelta(days=days_back)
        out: list[dict] = []
        # Every year in the window, inclusive — a set of {cutoff, today} would
        # silently skip whole middle years once days_back exceeds ~a year.
        for year in range(cutoff.year, today.year + 1):
            url = f"{HOUSE_BASE}/public_disc/financial-pdfs/{year}FD.zip"
            try:
                # Conditional-GET cache beside the seen-cache file: the yearly
                # ZIP is multi-MB and changes at most a few times a day.
                cache_dir = os.path.dirname(self.cache_file) or "."
                zip_cache = os.path.join(cache_dir, f"house_{year}FD.zip")
                blob = self.client.get_bytes_cached(url, zip_cache)
                with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                    xml_name = next((n for n in zf.namelist()
                                     if n.lower().endswith(".xml")), None)
                    if xml_name is None:
                        logger.warning("House FD ZIP for %d has no XML index.",
                                       year)
                        continue
                    # Decompression-bomb guard: the real index is a few MB. A
                    # corrupt/hostile ZIP could claim a huge uncompressed size
                    # and OOM the host on read; refuse implausibly large ones.
                    declared = zf.getinfo(xml_name).file_size
                    if declared > _MAX_INDEX_BYTES:
                        logger.warning("House FD index for %d is implausibly "
                                       "large (%d bytes); skipping.",
                                       year, declared)
                        continue
                    xml_text = zf.read(xml_name).decode("utf-8", "replace")
            except Exception as e:
                # Index failure -> nothing is marked seen; next run retries.
                logger.warning("Could not fetch/read House FD index for %d: %s",
                               year, e)
                continue
            for d in parse_house_index_xml(xml_text, year=year):
                fd = d.get("filing_date", "")
                if fd and not (cutoff.isoformat() <= fd <= today.isoformat()):
                    continue
                out.append(d)
        return out

    def fetch_house_transactions(
        self, d: dict
    ) -> list[CongressTransaction] | None:
        if not _HAVE_PYPDF:
            # Treat a missing dependency as a HARD failure: marking filings
            # seen here would permanently suppress them once pypdf IS
            # installed. (pip install pypdf)
            logger.error("pypdf is not installed — cannot parse House PTRs. "
                         "Install it: pip install pypdf")
            return None
        try:
            blob = self.client.get_bytes(d["url"])
        except Exception as e:
            logger.warning("Could not fetch House PTR %s (%s); will retry.",
                           d["doc_id"], e)
            return None
        # Content check BEFORE conflating "not parseable" with "permanently
        # unparseable": a 200 HTML body (maintenance page, or a PTR listed in
        # the index before its PDF is uploaded) is TRANSIENT — marking it seen
        # here would permanently swallow the filing. Only a real PDF may fall
        # through to the parse-or-skip-forever path below.
        if not blob[:1024].lstrip().startswith(b"%PDF"):
            logger.warning(
                "House PTR %s: response is not a PDF (interstitial or "
                "not-yet-uploaded document?); will retry.", d["doc_id"])
            return None
        try:
            reader = PdfReader(io.BytesIO(blob))
            if reader.is_encrypted:
                # House PTRs are RC4-encrypted with an empty user password.
                reader.decrypt("")
            pages = []
            for page in reader.pages:
                try:
                    pages.append(page.extract_text() or "")
                except Exception:
                    logger.warning("Unreadable page in House PTR %s (skipping "
                                   "page).", d["doc_id"])
            text = "\n".join(pages)
        except Exception:
            logger.warning("House PTR %s is not a readable PDF (paper scan or "
                           "format change); skipping permanently.", d["doc_id"])
            return []
        txs = parse_house_ptr_text(
            text, politician=d["politician"], filing_date=d["filing_date"],
            doc_id=d["doc_id"], link=d["url"],
        )
        if not txs:
            logger.info("House PTR %s (%s): no parseable transactions "
                        "(paper scan or non-stock filing).",
                        d["doc_id"], d["politician"])
        return txs

    def poll_house(self, days_back: int = 30,
                   today: _date | None = None,
                   max_filings: int | None = None) -> list[CongressTransaction]:
        descriptors = self.house_ptr_descriptors(days_back, today)
        new = self._process(descriptors, self.fetch_house_transactions, "house",
                            max_filings)
        if self.db is None:
            self._save_cache()
        return new

    # ---------------- Senate ----------------

    def senate_ptr_descriptors(self, days_back: int,
                               today: _date | None = None) -> list[dict]:
        today = today or _date.today()
        cutoff = today - timedelta(days=days_back)
        submitted_start = cutoff.strftime("%m/%d/%Y") + " 00:00:00"
        out: list[dict] = []
        start = 0
        while True:
            try:
                payload = self.client.senate_report_data(
                    start, _SENATE_PAGE_SIZE, submitted_start)
            except Exception as e:
                logger.warning("Senate eFD search failed at offset %d: %s",
                               start, e)
                break
            rows = payload.get("data", [])
            for row in rows:
                d = extract_senate_row(row)
                if d is not None:
                    out.append(d)
            start += len(rows)
            total = payload.get("recordsTotal", 0)
            if not rows or start >= total:
                break
        return out

    def fetch_senate_transactions(
        self, d: dict
    ) -> list[CongressTransaction] | None:
        if d.get("is_paper"):
            logger.info("Senate PTR %s (%s) is a paper filing (image); "
                        "skipping.", d["doc_id"], d["politician"])
            return []
        try:
            html_text = self.client.get_text(d["url"])
        except Exception as e:
            logger.warning("Could not fetch Senate PTR %s (%s); will retry.",
                           d["doc_id"], e)
            return None
        # Content check BEFORE trusting an empty parse: eFD session state can
        # rotate server-side mid-run, and follow_redirects turns the
        # agree-to-terms interstitial (or a maintenance page) into a 200 whose
        # parse yields [] — which would bulk-mark every remaining filing in
        # the run seen, permanently. Only a page that actually looks like a
        # PTR detail page may be marked seen when it parses empty.
        if "periodic transaction report" not in (html_text or "").lower():
            logger.warning(
                "Senate PTR %s (%s): response is not a PTR detail page "
                "(session interstitial or maintenance page?); will retry.",
                d["doc_id"], d["politician"])
            return None
        return parse_senate_ptr_html(
            html_text, politician=d["politician"],
            filing_date=d["filing_date"], uuid=d["doc_id"], link=d["url"],
        )

    def poll_senate(self, days_back: int = 30,
                    today: _date | None = None,
                    max_filings: int | None = None) -> list[CongressTransaction]:
        descriptors = self.senate_ptr_descriptors(days_back, today)
        new = self._process(descriptors, self.fetch_senate_transactions,
                            "senate", max_filings)
        if self.db is None:
            self._save_cache()
        return new

    # ---------------- shared processor (verbatim Form4Feed semantics) ----------------

    def _process(
        self,
        descriptors: list[dict],
        fetch: Callable[[dict], list[CongressTransaction] | None],
        label: str,
        max_filings: int | None = None,
    ) -> list[CongressTransaction]:
        new_transactions: list[CongressTransaction] = []
        processed = 0
        for i, d in enumerate(descriptors):
            accn = d["accession"]
            if accn in self._seen:
                continue
            # Per-run cap on NEW filings actually fetched (rate-limit safety,
            # same idea as the insider firehose's --max-filings). Skipped
            # filings stay unseen and are picked up by the next run.
            if max_filings is not None and processed >= max_filings:
                remaining = sum(1 for x in descriptors[i:]
                                if x["accession"] not in self._seen)
                logger.warning("%s: cap of %d new filing(s) reached; %d NOT "
                               "processed this run (next run resumes).",
                               label, max_filings, remaining)
                break
            processed += 1
            txs = fetch(d)
            if txs is None:
                # Hard fetch failure — leave unseen so the next poll retries.
                continue
            recorded_ok = True
            for t in txs:
                new_transactions.append(t)
                if self.db is not None:
                    try:
                        self.db.record_congress_transaction(t)
                    except Exception:
                        # Leave unseen on a DB write failure: the (accession,
                        # tx_seq) unique index makes a retry idempotent, so the
                        # next poll recovers the rows instead of losing them.
                        logger.exception(
                            "Could not record congress tx; leaving %s unseen "
                            "to retry.", accn)
                        recorded_ok = False
            if not recorded_ok:
                continue
            self._seen.add(accn)
            self._seen_order.append(accn)
            if self.db is not None:
                self.db.mark_filing_seen(accn)
            if txs:
                logger.info("New %s PTR %s (%s): %d transaction(s).",
                            label, accn, d["politician"], len(txs))
        return new_transactions

    def poll(self, chamber: str = "both",
             days_back: int = 30,
             today: _date | None = None,
             max_filings: int | None = None) -> list[CongressTransaction]:
        new: list[CongressTransaction] = []
        remaining = max_filings
        if chamber in ("house", "both"):
            before = len(self._seen)
            new.extend(self.poll_house(days_back, today, remaining))
            if remaining is not None:
                remaining = max(0, remaining - (len(self._seen) - before))
        if chamber in ("senate", "both"):
            new.extend(self.poll_senate(days_back, today, remaining))
        return new
