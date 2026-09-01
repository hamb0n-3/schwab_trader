from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("schwab_bot.edgar.parser")

# Transaction codes that represent discretionary open-market activity.
OPEN_MARKET_BUY = "P"
OPEN_MARKET_SELL = "S"
DISCRETIONARY_CODES = {OPEN_MARKET_BUY, OPEN_MARKET_SELL}


@dataclass
class InsiderTransaction:
    issuer_name: str
    ticker: str
    issuer_cik: str
    owner_name: str
    owner_cik: str
    is_director: bool
    is_officer: bool
    officer_title: str
    security_title: str
    transaction_code: str
    transaction_date: str
    shares: float
    price_per_share: float
    acquired_disposed: str   # "A" or "D"
    is_derivative: bool

    @property
    def dollar_value(self) -> float:
        return self.shares * self.price_per_share

    @property
    def is_open_market_buy(self) -> bool:
        return (not self.is_derivative
                and self.transaction_code == OPEN_MARKET_BUY
                and self.acquired_disposed == "A")

    @property
    def is_open_market_sell(self) -> bool:
        return (not self.is_derivative
                and self.transaction_code == OPEN_MARKET_SELL
                and self.acquired_disposed == "D")

    def describe(self) -> str:
        action = {"A": "ACQUIRED", "D": "DISPOSED"}.get(self.acquired_disposed, "?")
        role = "officer" if self.is_officer else ("director" if self.is_director else "10%")
        return (f"{self.owner_name} ({role}) {action} {self.shares:g} "
                f"{self.ticker} @ ${self.price_per_share:.2f} "
                f"(code {self.transaction_code}, ${self.dollar_value:,.0f})")


def _text(node: Optional[ET.Element], path: str, default: str = "") -> str:
    if node is None:
        return default
    el = node.find(path)
    if el is None:
        return default
    # Many fields wrap the actual value in a <value> child.
    val = el.find("value")
    if val is not None and val.text is not None:
        return val.text.strip()
    return (el.text or default).strip()


def _float(node: Optional[ET.Element], path: str, default: float = 0.0) -> float:
    raw = _text(node, path, "")
    if not raw:
        return default
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return default


def _bool_flag(node: Optional[ET.Element], path: str) -> bool:
    raw = _text(node, path, "").lower()
    return raw in ("1", "true")


def parse_form4(xml_text: str) -> list[InsiderTransaction]:
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError as e:
        logger.warning("Form 4 XML parse error: %s", e)
        return []

    # Some documents wrap content; the root is usually <ownershipDocument>.
    if root.tag != "ownershipDocument":
        found = root.find(".//ownershipDocument")
        if found is not None:
            root = found

    issuer = root.find("issuer")
    issuer_name = _text(issuer, "issuerName")
    ticker = _text(issuer, "issuerTradingSymbol").upper()
    issuer_cik = _text(issuer, "issuerCik")

    # Reporting owner(s). The owner CIK is the stable identifier for a PERSON
    # across all companies they're an insider of — names aren't unique, CIKs
    # are — so it's what person-mode keys on. Joint filings (e.g. a fund plus
    # the individual officer behind it) list SEVERAL reportingOwner blocks but
    # one shared transaction table, so we attribute transactions to the
    # first-listed owner while OR-ing the role flags across ALL owners — the
    # officers/directors filter must not drop a filing just because the
    # individual officer happens to be listed second.
    owners = root.findall("reportingOwner")
    first = owners[0] if owners else None
    owner_name = _text(first, "reportingOwnerId/rptOwnerName")
    owner_cik = _text(first, "reportingOwnerId/rptOwnerCik")
    is_director = False
    is_officer = False
    officer_title = ""
    for o in owners:
        rel = o.find("reportingOwnerRelationship")
        is_director = is_director or _bool_flag(rel, "isDirector")
        is_officer = is_officer or _bool_flag(rel, "isOfficer")
        officer_title = officer_title or _text(rel, "officerTitle")

    transactions: list[InsiderTransaction] = []

    def _collect(table_path: str, is_deriv: bool):
        table = root.find(table_path)
        if table is None:
            return
        tag = ("derivativeTransaction" if is_deriv else "nonDerivativeTransaction")
        for tx in table.findall(tag):
            coding = tx.find("transactionCoding")
            code = _text(coding, "transactionCode")
            amounts = tx.find("transactionAmounts")
            shares = _float(amounts, "transactionShares")
            price = _float(amounts, "transactionPricePerShare")
            ad = _text(amounts, "transactionAcquiredDisposedCode")
            date = _text(tx, "transactionDate")
            sec_title = _text(tx, "securityTitle")
            if shares <= 0:
                continue
            transactions.append(InsiderTransaction(
                issuer_name=issuer_name,
                ticker=ticker,
                issuer_cik=issuer_cik,
                owner_name=owner_name,
                owner_cik=owner_cik,
                is_director=is_director,
                is_officer=is_officer,
                officer_title=officer_title,
                security_title=sec_title,
                transaction_code=code,
                transaction_date=date,
                shares=shares,
                price_per_share=price,
                acquired_disposed=ad,
                is_derivative=is_deriv,
            ))

    _collect("nonDerivativeTable", is_deriv=False)
    _collect("derivativeTable", is_deriv=True)
    return transactions
