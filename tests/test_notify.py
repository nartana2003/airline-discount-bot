"""Link rewriting, route naming, and what the digest chooses to show."""

import base64
import unittest
from urllib.parse import parse_qs, urlsplit

from flightbot import notify

from tests._factories import row

GF = "https://www.google.com/travel/flights"


def params(url):
    return parse_qs(urlsplit(url).query)


class PriceSortedLinks(unittest.TestCase):
    def test_sort_is_rewritten_and_the_search_is_not(self):
        out = notify._by_price(f"{GF}?hl=en&tfs=CBwQAh&tfu=EgIIAQ")
        self.assertEqual(params(out)["tfs"], ["CBwQAh"])
        self.assertEqual(params(out)["tfu"], ["EgIIAg"])

    def test_the_rewritten_value_really_decodes_to_sort_by_price(self):
        out = notify._by_price(f"{GF}?tfs=CBwQAh&tfu=EgIIAQ")
        raw = base64.urlsafe_b64decode(params(out)["tfu"][0] + "==")
        self.assertEqual(list(raw), [0x12, 0x02, 0x08, 0x02])  # field 1 = 2 = Price

    def test_other_params_survive(self):
        out = notify._by_price(f"{GF}?hl=en&gl=au&curr=AUD&tfs=CBwQAh&tfu=EgIIAQ")
        self.assertEqual(params(out)["curr"], ["AUD"])
        self.assertEqual(params(out)["gl"], ["au"])

    def test_missing_tfu_is_added(self):
        self.assertEqual(params(notify._by_price(f"{GF}?tfs=CBwQAh"))["tfu"], ["EgIIAg"])

    def test_already_sorted_stays_sorted(self):
        self.assertEqual(params(notify._by_price(f"{GF}?tfs=A&tfu=EgIIAg"))["tfu"], ["EgIIAg"])

    def test_non_google_url_untouched(self):
        url = "https://example.com/x?tfs=A&tfu=EgIIAQ"
        self.assertEqual(notify._by_price(url), url)

    def test_text_query_link_untouched(self):
        url = f"{GF}?q=Flights%20from%20BNE"
        self.assertEqual(notify._by_price(url), url)

    def test_empty_and_none(self):
        self.assertEqual(notify._by_price(""), "")
        self.assertIsNone(notify._by_price(None))


class RouteNames(unittest.TestCase):
    def test_known_watch_wins(self):
        labels = {"bne-nrt": "BNE → NRT"}
        self.assertEqual(notify._route_label(row(900, "bne-nrt"), labels), "BNE → NRT")

    def test_deleted_route_named_from_the_row(self):
        r = row(965, "bne-hnd", from_id="BNE", to_id="HND")
        self.assertEqual(notify._route_label(r, {}), "BNE → HND")

    def test_deleted_route_without_airports_falls_back_to_the_slug(self):
        self.assertEqual(notify._route_label(row(965, "bne-hnd"), {}), "BNE → HND")

    def test_unslugged_id(self):
        self.assertEqual(notify._route_label(row(1, "weird"), {}), "weird")


class DigestPair(unittest.TestCase):
    """Which fare leads, and whether the period low is worth printing."""

    def pair(self, current, low):
        d = {"current": {"bne-nrt": current} if current else {}}
        return notify._digest_pair(d, low)

    def test_low_shown_when_genuinely_lower(self):
        now, low = self.pair(row(1034), row(717))
        self.assertEqual(now["price"], 1034)
        self.assertEqual(low["price"], 717)

    def test_low_hidden_when_equal_to_today(self):
        now, low = self.pair(row(965), row(965))
        self.assertEqual(now["price"], 965)
        self.assertIsNone(low)

    def test_without_a_current_price_the_low_leads(self):
        now, low = self.pair(None, row(717))
        self.assertEqual(now["price"], 717)
        self.assertIsNone(low)


if __name__ == "__main__":
    unittest.main()
