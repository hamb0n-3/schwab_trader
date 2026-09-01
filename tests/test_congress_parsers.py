import unittest

from signals.congress_parsers import (
    CongressTransaction,
    extract_senate_row,
    parse_amount_range,
    parse_house_index_xml,
    parse_house_ptr_text,
    parse_senate_ptr_html,
)


HOUSE_PTR_TEXT = """\
Name: Hon. Mark Alford
Status: Member
State/District: MO04
TRANSACTIONS
ID Owner Asset Transaction
Type
Date Notification
Date
Amount Cap.
Gains >
$200?
Apple Inc. - Common Stock (AAPL)
[ST]
S (partial) 03/16/2026 03/16/2026 $1,001 - $15,000
F S: New
S O: Putnam Investments
D : The full transaction included the following sales: lots A and B.
AT&T Inc. (T)  [ST] S (partial) 03/16/2026 03/16/2026 $1,001 - $15,000
SP Berkshire Hathaway Inc. Class B
(BRK.B) [ST] P 03/20/2026 03/21/2026 $15,001 -
$50,000
DIA - State Street Global Advisors SPDR Dow Jones Industrial Average
NYSEARCA: DIA [OT] P 03/22/2026 03/23/2026 Over $50,000,000
JT SPDR Portfolio S&P 500 ETF (SPYB) [ST] P 03/25/2026 03/25/2026 $1,001 - $15,000
JT SPDR Portfolio S&P 500 ETF (SPYB) [ST] P 03/25/2026 03/25/2026 $1,001 - $15,000
Filing ID #20034201
* For the complete list of assets, see the full filing.
"""

HOUSE_INDEX_XML = """\
﻿<FinancialDisclosure>
  <Member>
    <Prefix>Hon.</Prefix><Last>Alford</Last><First>Mark</First><Suffix/>
    <FilingType>P</FilingType>
    <StateDst>MO04</StateDst><Year>2026</Year>
    <FilingDate>3/31/2026</FilingDate>
    <DocID>20034201</DocID>
  </Member>
  <Member>
    <Last>Smith</Last><First>Jane</First>
    <FilingType>O</FilingType>
    <FilingDate>4/5/2026</FilingDate>
    <DocID>10001111</DocID>
  </Member>
  <Member>
    <Last>Doe</Last><First>John</First>
    <FilingType>P</FilingType>
    <FilingDate>4/5/2026</FilingDate>
    <DocID>20034999</DocID>
  </Member>
</FinancialDisclosure>
"""

SENATE_PTR_HTML = """\
<html><body>
<table class="table table-striped">
<thead><tr><th>#</th><th>Transaction Date</th><th>Owner</th><th>Ticker</th>
<th>Asset Name</th><th>Asset Type</th><th>Type</th><th>Amount</th>
<th>Comment</th></tr></thead>
<tbody>
<tr><td>1</td><td>05/12/2026</td><td>Self</td>
<td><a href="https://example.com/q?s=KHC">KHC</a></td>
<td>The Kraft Heinz Company</td><td>Stock</td><td>Purchase</td>
<td>$1,001 - $15,000</td><td>--</td></tr>
<tr><td>2</td><td>05/13/2026</td><td>Spouse</td><td>--</td>
<td>Some Municipal Bond Fund</td><td>Municipal Security</td>
<td>Sale (Partial)</td><td>$50,001 - $100,000</td><td>--</td></tr>
<tr><td>3</td><td>05/14/2026</td><td>Joint</td>
<td><a href="#">NVDA</a></td><td>NVIDIA Corp</td><td>Stock</td>
<td>Purchase</td><td>$250,001 - $500,000</td><td>--</td></tr>
</tbody></table>
</body></html>
"""


class TestAmountRange(unittest.TestCase):
    def test_normal_range(self):
        self.assertEqual(parse_amount_range("$1,001 - $15,000"), (1001.0, 15000.0))

    def test_open_ended_over(self):
        self.assertEqual(parse_amount_range("Over $50,000,000"), (50000000.0, None))

    def test_open_ended_dash(self):
        self.assertEqual(parse_amount_range("$1,000,001 -"), (1000001.0, None))

    def test_garbage(self):
        self.assertEqual(parse_amount_range("n/a"), (None, None))
        self.assertEqual(parse_amount_range(""), (None, None))
        self.assertEqual(parse_amount_range(None), (None, None))


class TestHouseIndex(unittest.TestCase):
    def test_ptr_only_and_dates(self):
        out = parse_house_index_xml(HOUSE_INDEX_XML, year=2026)
        self.assertEqual(len(out), 2)  # the 'O' filing is excluded
        d = out[0]
        self.assertEqual(d["accession"], "house-20034201")
        self.assertEqual(d["politician"], "Mark Alford")
        self.assertEqual(d["filing_date"], "2026-03-31")  # M/D/YYYY normalized
        self.assertIn("/2026/20034201.pdf", d["url"])
        self.assertEqual(out[1]["filing_date"], "2026-04-05")

    def test_malformed_xml(self):
        self.assertEqual(parse_house_index_xml("<broken", year=2026), [])


class TestHousePtrText(unittest.TestCase):
    def parse(self):
        return parse_house_ptr_text(
            HOUSE_PTR_TEXT, politician="Mark Alford",
            filing_date="2026-03-31", doc_id="20034201",
            link="https://x/20034201.pdf")

    def test_row_count(self):
        # AAPL, T, BRK.B, DIA, SPYB, SPYB
        self.assertEqual(len(self.parse()), 6)

    def test_no_header_leakage(self):
        # Table-header fragments ('Date Notification', 'Amount', ...) must
        # not bleed into the first row's asset description.
        self.assertNotIn("Notification", self.parse()[0].asset_description)

    def test_wrapped_asset_and_partial_sale(self):
        t = self.parse()[0]
        self.assertEqual(t.ticker, "AAPL")
        self.assertEqual(t.tx_type, "S")
        self.assertEqual(t.raw_type, "S (partial)")
        self.assertEqual(t.owner, "")           # blank owner = Self
        self.assertEqual(t.asset_type, "ST")
        self.assertEqual(t.transaction_date, "2026-03-16")
        self.assertEqual((t.amount_min, t.amount_max), (1001.0, 15000.0))

    def test_single_line_row(self):
        t = self.parse()[1]
        self.assertEqual(t.ticker, "T")
        self.assertEqual(t.tx_type, "S")

    def test_wrapped_amount_and_dotted_ticker(self):
        t = self.parse()[2]
        self.assertEqual(t.ticker, "BRK.B")
        self.assertEqual(t.owner, "SP")
        self.assertEqual(t.tx_type, "P")
        # the range wrapped across lines: '$15,001 -' / '$50,000'
        self.assertEqual((t.amount_min, t.amount_max), (15001.0, 50000.0))

    def test_tickerless_ot_row(self):
        t = self.parse()[3]
        self.assertEqual(t.ticker, "")          # NYSEARCA: DIA is not (TICK)
        self.assertEqual(t.asset_type, "OT")
        self.assertFalse(t.is_stock)
        self.assertEqual((t.amount_min, t.amount_max), (50000000.0, None))

    def test_duplicate_rows_get_distinct_seq(self):
        rows = self.parse()
        a, b = rows[4], rows[5]                 # the two SPYB rows
        self.assertEqual(a.ticker, "SPYB")
        self.assertEqual(b.ticker, "SPYB")
        self.assertNotEqual(a.tx_seq, b.tx_seq)
        self.assertEqual(a.accession, b.accession)

    def test_paper_scan_yields_empty(self):
        self.assertEqual(parse_house_ptr_text(
            "", politician="X", filing_date="", doc_id="1", link=""), [])
        self.assertEqual(parse_house_ptr_text(
            "garbage with no anchor", politician="X", filing_date="",
            doc_id="1", link=""), [])


## Verbatim pypdf extraction of a LIVE e-filed PTR (Chip Roy, DocID 20034762,
## fetched 2026-06-11): section headings degrade to NUL-glyph stubs and the
## date/date/amount columns run together with no spaces.
HOUSE_PTR_TEXT_REAL = (
    "P\x00\x00\x00\x00\x00\x00\x00 T\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00 "
    "R\x00\x00\x00\x00\x00\n"
    "Clerk of the House of Representatives • Legislative Resource Center • "
    "B81 Cannon Building • Washington, DC 20515\n"
    "F\x00\x00\x00\x00 I\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\n"
    "Name: Hon. Chip Roy\n"
    "Status: Member\n"
    "State/District:TX21\n"
    "T\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\n"
    "ID Owner Asset Transaction\n"
    "Type\n"
    "Date Notification\n"
    "Date\n"
    "Amount Cap.\n"
    "Gains >\n"
    "$200?\n"
    "SP Atlas Energy Solutions Inc. Common\n"
    "Stock (AESI) [ST]\n"
    "S (partial) 05/13/202605/13/2026$100,001 -\n"
    "$250,000\n"
    "F\x00\x00\x00\x00\x00 S\x00\x00\x00\x00\x00: New\n"
    "* For the complete list of asset type abbreviations, please visit "
    "https://fd.house.gov/reference/asset-type-codes.aspx.\n"
    "I\x00\x00\x00\x00\x00\x00 P\x00\x00\x00\x00\x00 O\x00\x00\x00\x00\x00"
    "\x00\x00\x00\n"
    " Yes  No\n"
    "C\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00 \x00\x00\x00 "
    "S\x00\x00\x00\x00\x00\x00\x00\x00\n"
    " I CERTIFY that the statements I have made on the attached Periodic "
    "Transaction Report are true, complete, and correct to the best of\n"
    "my knowledge and belief. Further, I CERTIFY that I have disclosed all "
    "transactions as required by the STOCK Act.\n"
    "Digitally Signed: Hon. Chip Roy , 06/10/2026\n"
    "Filing ID #20034762"
)


class TestHousePtrTextRealLayout(unittest.TestCase):

    def test_real_extraction(self):
        txs = parse_house_ptr_text(
            HOUSE_PTR_TEXT_REAL, politician="Chip Roy",
            filing_date="2026-06-10", doc_id="20034762", link="")
        self.assertEqual(len(txs), 1)
        t = txs[0]
        self.assertEqual(t.ticker, "AESI")
        self.assertEqual(t.owner, "SP")
        self.assertEqual(t.tx_type, "S")
        self.assertEqual(t.raw_type, "S (partial)")
        self.assertEqual(t.transaction_date, "2026-05-13")
        self.assertEqual(t.notification_date, "2026-05-13")
        self.assertEqual((t.amount_min, t.amount_max), (100001.0, 250000.0))
        self.assertEqual(t.asset_type, "ST")
        # certification boilerplate must not leak into the asset cell
        self.assertNotIn("CERTIFY", t.asset_description)


class TestSenateRow(unittest.TestCase):
    def test_electronic(self):
        row = ["Gary C", "Peters", "Peters, Gary (Senator)",
               '<a href="/search/view/ptr/be9bb561-8290-4364-85b4-06a59ef0ec01/"'
               ' target="_blank">Periodic Transaction Report for 06/11/2026</a>',
               "06/11/2026"]
        d = extract_senate_row(row)
        self.assertEqual(d["accession"],
                         "senate-be9bb561-8290-4364-85b4-06a59ef0ec01")
        self.assertEqual(d["politician"], "Gary C Peters")
        self.assertEqual(d["filing_date"], "2026-06-11")
        self.assertFalse(d["is_paper"])

    def test_paper(self):
        row = ["A", "B", "x",
               '<a href="/search/view/paper/12ab-34cd/">PTR</a>', "06/01/2026"]
        self.assertTrue(extract_senate_row(row)["is_paper"])

    def test_malformed(self):
        self.assertIsNone(extract_senate_row(["only", "two"]))
        self.assertIsNone(extract_senate_row(
            ["A", "B", "x", "no link here", "06/01/2026"]))


class TestSenatePtrHtml(unittest.TestCase):
    def parse(self):
        return parse_senate_ptr_html(
            SENATE_PTR_HTML, politician="Gary C Peters",
            filing_date="2026-06-11", uuid="be9bb561", link="https://x/")

    def test_rows(self):
        txs = self.parse()
        self.assertEqual(len(txs), 3)
        khc = txs[0]
        self.assertEqual(khc.ticker, "KHC")
        self.assertEqual(khc.tx_type, "P")
        self.assertEqual(khc.owner, "Self")
        self.assertEqual(khc.transaction_date, "2026-05-12")
        self.assertEqual((khc.amount_min, khc.amount_max), (1001.0, 15000.0))
        self.assertTrue(khc.is_stock)

    def test_no_ticker_and_sale(self):
        t = self.parse()[1]
        self.assertEqual(t.ticker, "")
        self.assertEqual(t.tx_type, "S")
        self.assertEqual(t.raw_type, "Sale (Partial)")
        self.assertFalse(t.is_stock)

    def test_truncated_html(self):
        self.assertEqual(parse_senate_ptr_html(
            "<table><tr><td>1</td>", politician="X", filing_date="",
            uuid="u", link=""), [])


if __name__ == "__main__":
    unittest.main()
