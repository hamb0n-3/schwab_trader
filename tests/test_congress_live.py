import os
import unittest

LIVE = os.environ.get("CONGRESS_LIVE_TEST") == "1"


@unittest.skipUnless(LIVE, "set CONGRESS_LIVE_TEST=1 to hit live endpoints")
class TestLiveSources(unittest.TestCase):
    def setUp(self):
        from signals.congress_client import CongressClient
        self.client = CongressClient(max_per_second=2.0)

    def tearDown(self):
        self.client.close()

    def test_house_index_and_one_pdf(self):
        from signals.congress_feed import CongressFeed
        feed = CongressFeed(self.client, cache_file="")
        descriptors = feed.house_ptr_descriptors(days_back=45)
        self.assertGreater(len(descriptors), 0,
                           "no House PTRs in the last 45 days?")
        # Try e-filed PTRs (DocID starting with '2') until one parses; the
        # point is "the pipeline runs without raising", not row counts.
        efiled = [d for d in descriptors if d["doc_id"].startswith("2")]
        self.assertGreater(len(efiled), 0)
        txs = feed.fetch_house_transactions(efiled[0])
        self.assertIsNotNone(txs, "PDF fetch failed (network?)")

    def test_senate_search_and_one_ptr(self):
        from signals.congress_feed import CongressFeed
        feed = CongressFeed(self.client, cache_file="")
        descriptors = feed.senate_ptr_descriptors(days_back=45)
        # Senate PTR volume is lower; an empty window is possible but rare.
        for d in descriptors:
            self.assertTrue(d["accession"].startswith("senate-"))
            self.assertRegex(d["filing_date"], r"^\d{4}-\d{2}-\d{2}$")
        electronic = [d for d in descriptors if not d["is_paper"]]
        if electronic:
            txs = feed.fetch_senate_transactions(electronic[0])
            self.assertIsNotNone(txs, "detail fetch failed (network?)")


if __name__ == "__main__":
    unittest.main()
