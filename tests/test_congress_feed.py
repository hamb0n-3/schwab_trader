import json
import os
import tempfile
import unittest
from datetime import date

from signals.congress_feed import CongressFeed
from signals.congress_parsers import CongressTransaction


def make_tx(doc_id, seq=0):
    return CongressTransaction(
        chamber="senate", politician="Test Person", owner="Self",
        ticker="KHC", asset_description="Kraft Heinz", asset_type="Stock",
        tx_type="P", raw_type="Purchase", transaction_date="2026-05-12",
        notification_date="", filing_date="2026-06-01", amount_min=1001.0,
        amount_max=15000.0, amount_raw="$1,001 - $15,000",
        doc_id=doc_id, link="", tx_seq=seq,
    )


class StubFeed(CongressFeed):

    def __init__(self, behaviors, **kw):
        super().__init__(client=None, **kw)
        self.behaviors = behaviors      # accession -> "ok" | "fail" | "empty"
        self.fetches = []

    def _stub_fetch(self, d):
        self.fetches.append(d["accession"])
        mode = self.behaviors[d["accession"]]
        if mode == "fail":
            return None
        if mode == "empty":
            return []
        return [make_tx(d["doc_id"])]


def descriptors(*accns):
    return [{"accession": a, "doc_id": a.split("-", 1)[1],
             "politician": "Test Person", "filing_date": "2026-06-01",
             "url": "", "is_paper": False} for a in accns]


class FakeDB:

    def __init__(self, record_raises=False):
        self.record_raises = record_raises
        self.seen: set[str] = set()
        self.recorded: list = []

    def seen_accessions(self):
        return set(self.seen)

    def record_congress_transaction(self, tx):
        if self.record_raises:
            raise RuntimeError("disk full")
        self.recorded.append(tx)

    def mark_filing_seen(self, accn):
        self.seen.add(accn)


class TestDbRecordFailure(unittest.TestCase):
    def test_record_failure_leaves_filing_unseen_for_retry(self):
        # A DB write failure must NOT mark the filing seen: the unique index
        # makes a retry idempotent, so the next poll should recover it rather
        # than dropping the transactions permanently.
        db = FakeDB(record_raises=True)
        feed = StubFeed({"senate-x": "ok"}, cache_file="", db=db)
        feed._process(descriptors("senate-x"), feed._stub_fetch, "senate")
        self.assertNotIn("senate-x", feed._seen)
        self.assertNotIn("senate-x", db.seen)
        # next poll: DB recovers, filing is retried and now marked seen
        db.record_raises = False
        feed._process(descriptors("senate-x"), feed._stub_fetch, "senate")
        self.assertIn("senate-x", feed._seen)
        self.assertEqual(len(db.recorded), 1)


class TestSeenSemantics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self.tmp.name, "seen.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_hard_failure_left_unseen_and_retried(self):
        feed = StubFeed({"senate-a": "fail", "senate-b": "ok"},
                        cache_file=self.cache)
        out = feed._process(descriptors("senate-a", "senate-b"),
                            feed._stub_fetch, "senate")
        self.assertEqual(len(out), 1)                  # only b parsed
        self.assertNotIn("senate-a", feed._seen)       # a stays unseen
        self.assertIn("senate-b", feed._seen)
        # next poll: a is retried (now succeeding), b is skipped
        feed.behaviors["senate-a"] = "ok"
        feed.fetches.clear()
        out2 = feed._process(descriptors("senate-a", "senate-b"),
                             feed._stub_fetch, "senate")
        self.assertEqual(feed.fetches, ["senate-a"])   # b not re-fetched
        self.assertEqual(len(out2), 1)

    def test_empty_marks_seen(self):
        feed = StubFeed({"senate-paper": "empty"}, cache_file=self.cache)
        out = feed._process(descriptors("senate-paper"), feed._stub_fetch, "s")
        self.assertEqual(out, [])
        self.assertIn("senate-paper", feed._seen)      # never re-fetched

    def test_json_cache_roundtrip_and_cap(self):
        feed = StubFeed({}, cache_file=self.cache)
        feed._seen_order = [f"senate-{i}" for i in range(6000)]
        feed._seen = set(feed._seen_order)
        feed._save_cache()
        with open(self.cache) as f:
            saved = json.load(f)
        self.assertEqual(len(saved), 5000)
        # capped by RECENCY: the newest entries survive
        self.assertEqual(saved[-1], "senate-5999")
        self.assertEqual(saved[0], "senate-1000")
        # reload round-trip
        feed2 = StubFeed({}, cache_file=self.cache)
        self.assertEqual(len(feed2._seen), 5000)
        self.assertIn("senate-5999", feed2._seen)


class _FakeHouseClient:

    def __init__(self):
        self.zips: list[str] = []

    def get_bytes(self, url):
        self.zips.append(url)
        import io, zipfile
        xml = (
            '<FinancialDisclosure><Member>'
            '<Last>Doe</Last><First>Jan</First>'
            '<FilingType>P</FilingType>'
            '<FilingDate>1/05/2026</FilingDate>'
            '<DocID>20030001</DocID></Member></FinancialDisclosure>'
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("2026FD.xml", xml)
        return buf.getvalue()

    def get_bytes_cached(self, url, cache_path):
        return self.get_bytes(url)


class TestHouseDescriptorWindow(unittest.TestCase):
    def test_year_span_and_date_filter(self):
        client = _FakeHouseClient()
        feed = CongressFeed(client=client, cache_file="")
        # window spans the year boundary -> both 2025 and 2026 ZIPs fetched
        out = feed.house_ptr_descriptors(days_back=30, today=date(2026, 1, 10))
        self.assertEqual(len(client.zips), 2)
        self.assertTrue(any("2025FD.zip" in u for u in client.zips))
        self.assertTrue(any("2026FD.zip" in u for u in client.zips))
        # filing inside the window survives the filter (appears twice — once
        # per fake ZIP — which is fine: the seen-cache dedups by accession)
        self.assertTrue(all(d["accession"] == "house-20030001" for d in out))
        # window that excludes Jan 5 filters it out
        out2 = feed.house_ptr_descriptors(days_back=2, today=date(2026, 3, 1))
        self.assertEqual(out2, [])

    def test_multi_year_window_includes_middle_years(self):
        # days_back > a year must NOT skip whole middle years: 800 days back
        # from 2026-06-11 reaches into 2024 -> 2024, 2025 AND 2026 ZIPs.
        client = _FakeHouseClient()
        feed = CongressFeed(client=client, cache_file="")
        feed.house_ptr_descriptors(days_back=800, today=date(2026, 6, 11))
        years = sorted(u.split("/")[-1][:4] for u in client.zips)
        self.assertEqual(years, ["2024", "2025", "2026"])


class TestMaxFilingsCap(unittest.TestCase):
    def test_cap_processes_first_n_and_leaves_rest_unseen(self):
        feed = StubFeed({"senate-a": "ok", "senate-b": "ok", "senate-c": "ok"},
                        cache_file="")
        out = feed._process(descriptors("senate-a", "senate-b", "senate-c"),
                            feed._stub_fetch, "senate", max_filings=2)
        self.assertEqual(len(out), 2)
        self.assertEqual(feed.fetches, ["senate-a", "senate-b"])
        self.assertNotIn("senate-c", feed._seen)   # resumes next run
        out2 = feed._process(descriptors("senate-a", "senate-b", "senate-c"),
                             feed._stub_fetch, "senate", max_filings=2)
        self.assertEqual(len(out2), 1)             # only c remained


class TestLegacyEdgarImportUnguarded(unittest.TestCase):
    def test_edgar_import_survives_preexisting_congress_rows(self):
        import types
        from db import Database

        with tempfile.TemporaryDirectory() as tmp:
            edgar_path = os.path.join(tmp, "edgar_seen.json")
            with open(edgar_path, "w") as f:
                json.dump(["0001-23-000001", "0001-23-000002"], f)
            with Database(os.path.join(tmp, "t.db")) as db:
                db.mark_filing_seen("senate-uuid-1")    # congress ran first
                cfg = types.SimpleNamespace(daily_state_file="")
                db.import_legacy(cfg, edgar_cache_path=edgar_path)
                seen = db.seen_accessions()
                self.assertIn("0001-23-000001", seen)
                self.assertIn("0001-23-000002", seen)
                self.assertIn("senate-uuid-1", seen)


if __name__ == "__main__":
    unittest.main()
