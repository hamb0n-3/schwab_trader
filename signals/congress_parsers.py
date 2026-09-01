from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser

logger = logging.getLogger("schwab_bot.congress.parser")

HOUSE_BASE = "https://disclosures-clerk.house.gov"
SENATE_BASE = "https://efdsearch.senate.gov"

# Normalized transaction types.
PURCHASE = "P"
SALE = "S"
EXCHANGE = "E"


@dataclass
class CongressTransaction:
    chamber: str             # "house" | "senate"
    politician: str          # "Mark Alford" / "Gary C Peters"
    owner: str               # "", "SP", "JT", "DC" (house) / "Self", "Spouse", ...
    ticker: str              # upper-case; "" when the filing has none
    asset_description: str
    asset_type: str          # house bracket code ("ST", "OT", ...) / senate ("Stock", ...)
    tx_type: str             # normalized: P / S / E
    raw_type: str            # as filed: "S (partial)", "Sale (Full)", ...
    transaction_date: str    # ISO YYYY-MM-DD ("" if unparseable)
    notification_date: str   # ISO; "" for senate
    filing_date: str         # ISO
    amount_min: float | None
    amount_max: float | None  # None for open-ended ("Over $X")
    amount_raw: str
    doc_id: str              # house DocID / senate uuid
    link: str                # PDF / detail-page URL
    tx_seq: int              # row index within the filing (part of the dedup key)

    @property
    def accession(self) -> str:
        return f"{self.chamber}-{self.doc_id}"

    @property
    def is_stock(self) -> bool:
        return self.asset_type in ("ST", "Stock")

    @property
    def is_purchase(self) -> bool:
        return self.tx_type == PURCHASE

    @property
    def is_sale(self) -> bool:
        return self.tx_type == SALE

    def describe(self) -> str:
        amt = self.amount_raw or "?"
        tick = self.ticker or self.asset_description[:30] or "?"
        verb = {"P": "bought", "S": "sold", "E": "exchanged"}.get(self.tx_type, "?")
        return (f"{self.politician} ({self.chamber}) {verb} {amt} of {tick} "
                f"on {self.transaction_date or '?'}")


# --------------------------- shared helpers ---------------------------

def parse_amount_range(raw: str) -> tuple[float | None, float | None]:
    try:
        nums = [float(n.replace(",", ""))
                for n in re.findall(r"\$([\d,]+)", raw or "")]
    except ValueError:
        return (None, None)
    if not nums:
        return (None, None)
    if len(nums) == 1:
        return (nums[0], None)        # open-ended ("Over $X" / "$X -")
    return (nums[0], nums[1])


def _iso_date(raw: str) -> str:
    raw = (raw or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _normalize_tx_type(raw: str) -> str:
    t = (raw or "").strip().lower()
    if t.startswith("p"):
        return PURCHASE
    if t.startswith("s"):
        return SALE
    if t.startswith("e"):
        return EXCHANGE
    return ""


# --------------------------- House: FD.xml index ---------------------------

def parse_house_index_xml(xml_text: str, *, year: int) -> list[dict]:
    try:
        # The live file carries a UTF-8 BOM; lstrip it defensively.
        root = ET.fromstring(xml_text.lstrip("﻿").strip())
    except ET.ParseError as e:
        logger.warning("House FD index XML parse error: %s", e)
        return []

    out: list[dict] = []
    for m in root.iter("Member"):
        if (m.findtext("FilingType") or "").strip() != "P":
            continue
        doc_id = (m.findtext("DocID") or "").strip()
        # DocIDs are numeric. Reject anything else: the value comes from a
        # fetched document and is interpolated into the PTR URL path, so a
        # malformed/hostile DocID ('../x', a full URL) must never reach it.
        if not doc_id.isdigit():
            if doc_id:
                logger.warning("Skipping House filing with non-numeric "
                               "DocID %r.", doc_id)
            continue
        first = (m.findtext("First") or "").strip()
        last = (m.findtext("Last") or "").strip()
        out.append({
            "accession": f"house-{doc_id}",
            "doc_id": doc_id,
            "politician": " ".join(p for p in (first, last) if p),
            "filing_date": _iso_date(m.findtext("FilingDate") or ""),
            "url": f"{HOUSE_BASE}/public_disc/ptr-pdfs/{year}/{doc_id}.pdf",
        })
    return out


# --------------------------- House: PTR PDF text ---------------------------

# The unambiguous right-hand columns of a transaction row: type, transaction
# date, notification date, amount. Everything BEFORE a match is the
# owner/asset cell. Amounts may wrap mid-range, so the amount alternatives
# include a trailing "$X -" / "$X" fragment completed by _AMOUNT_TAIL_RE.
_CORE_RE = re.compile(
    # Column separators are \s* (not \s+): the Clerk's PDF renderer often
    # emits the date/date/amount columns run together with NO space between
    # them ("05/13/202605/13/2026$100,001 -" — verified live).
    r"(?P<rtype>P|S(?:\s*\(partial\))?|E)\s*"
    r"(?P<date>\d{2}/\d{2}/\d{4})\s*"
    r"(?P<notif>\d{2}/\d{2}/\d{4})\s*"
    r"(?P<amount>Over\s+\$[\d,]+|\$[\d,]+\s*-\s*\$[\d,]+|\$[\d,]+\s*-?)"
)
_AMOUNT_TAIL_RE = re.compile(r"^\s*-?\s*(\$[\d,]+)")   # wrapped 2nd half of a range
_OWNER_RE = re.compile(r"^(SP|JT|DC)\b")
_TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9./-]{0,11})\)")
# Parenthesized words that are NOT tickers but match _TICKER_RE: entity-form
# suffixes and roman numerals that can follow the real ticker in an asset
# name ("... Fund II (AAPL) (REIT)"). A false grab wouldn't just fail a price
# lookup — it could price the WRONG stock. Deliberately EXCLUDES strings that
# are also real major tickers (V=Visa, AG=First Majestic, ETN=Eaton, SA=
# Seabridge): for those, losing a genuine ticker is worse than the rare
# suffix collision, and the last-parenthetical heuristic already prefers the
# trailing real ticker.
_TICKER_BLOCKLIST = {
    "REIT", "ETF", "ADR", "LLC", "LP", "LTD", "INC", "CORP", "PLC",
    "II", "III", "IV", "VI", "VII",
}
_ATYPE_RE = re.compile(r"\[([A-Z]{2})\]")
_SKIP_RE = re.compile(
    r"^(F\s*S\s*:|S\s*O\s*:|D\s*:|L\s*:|C\s*:"          # sub-detail lines
    r"|ID\s+Owner\s+Asset|Type$|Date(\s+Notification)?$|Notification|Amount|Gains|\$200\?"
    r"|Filing\s+ID\s+#|\*\s*For the complete list|Name:|Status:|State/District:"
    r"|Asset\s+class\s+details)"
)


def parse_house_ptr_text(text: str, *, politician: str, filing_date: str,
                         doc_id: str, link: str) -> list[CongressTransaction]:
    if not text:
        return []
    # The PDFs' small-caps section headings use a subset font with an
    # incomplete ToUnicode map: "TRANSACTIONS" extracts as "T" followed by
    # NUL bytes (verified live). Strip NULs so noise lines like
    # "F\x00... S\x00...: New" collapse back to their skippable forms.
    text = text.replace("\x00", "")
    lines = text.splitlines()
    # Anchor on the table HEADER ("ID Owner Asset ...") — it extracts cleanly,
    # unlike the section heading. Accept a literal TRANSACTIONS heading too
    # (older renderers / our fixtures). Without either anchor this isn't a
    # parseable PTR (paper scan).
    start = next((i for i, ln in enumerate(lines)
                  if re.match(r"\s*(TRANSACTIONS\b|ID\s+Owner\s+Asset)",
                              ln.strip(), re.IGNORECASE)), None)
    if start is None:
        return []

    txs: list[CongressTransaction] = []
    buf = ""
    pending_amount_open = False   # last row's amount ended with '$X -' (wrapped)

    def _emit(cell: str, m: re.Match) -> None:
        try:
            owner = ""
            cell = cell.strip()
            om = _OWNER_RE.match(cell)
            if om:
                owner = om.group(1)
                cell = cell[om.end():].strip()
            tickers = [t for t in _TICKER_RE.findall(cell)
                       if t.upper() not in _TICKER_BLOCKLIST]
            ticker = tickers[-1].upper() if tickers else ""
            am = _ATYPE_RE.search(cell)
            asset_type = am.group(1) if am else ""
            # The asset description is the cell minus the bracket code.
            desc = _ATYPE_RE.sub("", cell).strip()
            raw_type = re.sub(r"\s+", " ", m.group("rtype"))
            lo, hi = parse_amount_range(m.group("amount"))
            txs.append(CongressTransaction(
                chamber="house",
                politician=politician,
                owner=owner,
                ticker=ticker,
                asset_description=desc,
                asset_type=asset_type,
                tx_type=_normalize_tx_type(raw_type),
                raw_type=raw_type,
                transaction_date=_iso_date(m.group("date")),
                notification_date=_iso_date(m.group("notif")),
                filing_date=filing_date,
                amount_min=lo,
                amount_max=hi,
                amount_raw=re.sub(r"\s+", " ", m.group("amount")).strip(),
                doc_id=doc_id,
                link=link,
                tx_seq=len(txs),
            ))
        except Exception:
            logger.exception("House PTR row parse failed in %s (skipping row).",
                             doc_id)

    for ln in lines[start + 1:]:
        ln = ln.strip()
        if not ln or _SKIP_RE.match(ln):
            continue
        # A wrapped amount: previous row ended '$X -' and this line starts
        # with the closing '$Y'. Complete the previous row's range.
        if pending_amount_open and txs:
            tail = _AMOUNT_TAIL_RE.match(ln)
            if tail:
                hi = parse_amount_range(tail.group(1))[0]
                prev = txs[-1]
                prev.amount_max = hi
                prev.amount_raw = f"{prev.amount_raw.rstrip(' -')} - {tail.group(1)}"
                ln = ln[tail.end():].strip()
            pending_amount_open = False
            if not ln:
                continue
        buf = f"{buf} {ln}".strip() if buf else ln
        while True:
            m = _CORE_RE.search(buf)
            if not m:
                break
            _emit(buf[:m.start()], m)
            amt = m.group("amount").rstrip()
            # Arm the wrapped-amount patch for a trailing-dash range half
            # ("$15,001 -") AND for a bare "$X": the dash itself can wrap to
            # the next line ("$100,001" / "- $250,000"). Deliberate trade-off:
            # if a NEXT LINE legitimately started with a dollar figure it
            # would be mis-attached — unobserved in real PTRs (amounts are
            # always ranges or "Over $X", and table-header "$200?" lines are
            # eaten by _SKIP_RE before this branch).
            pending_amount_open = amt.endswith("-") or (
                "-" not in amt and not amt.lower().startswith("over"))
            buf = buf[m.end():].strip()
    return txs


# --------------------------- Senate: search rows ---------------------------

_SENATE_LINK_RE = re.compile(
    r'href="(/search/view/(ptr|paper)/([0-9a-f-]+)/?)"', re.IGNORECASE
)


def extract_senate_row(row: list) -> dict | None:
    try:
        first, last = str(row[0]).strip(), str(row[1]).strip()
        link_html, received = str(row[3]), str(row[4]).strip()
    except (IndexError, TypeError):
        logger.warning("Unexpected Senate search row shape: %r", row)
        return None
    m = _SENATE_LINK_RE.search(link_html)
    if not m:
        logger.warning("No PTR link in Senate row for %s %s.", first, last)
        return None
    path, kind, uuid = m.group(1), m.group(2).lower(), m.group(3)
    return {
        "accession": f"senate-{uuid}",
        "doc_id": uuid,
        "politician": " ".join(p for p in (first, last) if p),
        "filing_date": _iso_date(received),
        "url": f"{SENATE_BASE}{path}",
        "is_paper": kind == "paper",
    }


# --------------------------- Senate: PTR detail HTML ---------------------------

class _TableParser(HTMLParser):

    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._in_td = False
        self._cells: list[str] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._cells = []
        elif tag == "td" and self._cells is not None:
            self._in_td = True
            self._text = []

    def handle_endtag(self, tag):
        if tag == "td" and self._in_td:
            self._in_td = False
            self._cells.append(" ".join("".join(self._text).split()))
        elif tag == "tr" and self._cells is not None:
            if self._cells:
                self.rows.append(self._cells)
            self._cells = None

    def handle_data(self, data):
        if self._in_td:
            self._text.append(data)


def parse_senate_ptr_html(html_text: str, *, politician: str, filing_date: str,
                          uuid: str, link: str) -> list[CongressTransaction]:
    try:
        p = _TableParser()
        p.feed(html_text or "")
    except Exception:
        logger.warning("Senate PTR HTML parse error for %s.", uuid)
        return []

    txs: list[CongressTransaction] = []
    for cells in p.rows:
        if len(cells) < 8:
            continue
        tx_date = _iso_date(cells[1])
        raw_type = cells[6]
        tx_type = _normalize_tx_type(raw_type)
        if not tx_date or not tx_type:
            continue  # header rows / malformed rows
        ticker = cells[3].strip().upper()
        if ticker in ("--", "-", "N/A"):
            ticker = ""
        lo, hi = parse_amount_range(cells[7])
        txs.append(CongressTransaction(
            chamber="senate",
            politician=politician,
            owner=cells[2],
            ticker=ticker,
            asset_description=cells[4],
            asset_type=cells[5],
            tx_type=tx_type,
            raw_type=raw_type,
            transaction_date=tx_date,
            notification_date="",
            filing_date=filing_date,
            amount_min=lo,
            amount_max=hi,
            amount_raw=cells[7],
            doc_id=uuid,
            link=link,
            tx_seq=len(txs),
        ))
    return txs
