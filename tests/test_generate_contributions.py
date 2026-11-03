import importlib.util
import sys
import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("contributions", ROOT / "scripts" / "generate_contributions.py")
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def sample_data(window, count_for_day=None, viewer="salahsaeed19"):
    count_for_day = count_for_day or (lambda _day, _index: 0)
    days = [
        {"date": day.isoformat(), "contributionCount": count_for_day(day, index), "weekday": (day.weekday() + 1) % 7}
        for index, day in enumerate(window.dates)
    ]
    return {
        "viewer": {"login": viewer},
        "user": {
            "contributionsCollection": {
                "contributionCalendar": {
                    "totalContributions": sum(day["contributionCount"] for day in days),
                    "weeks": [{"contributionDays": days}],
                }
            }
        },
    }


class ContributionGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.window = module.contribution_window(date(2026, 11, 3), "UTC")

    def test_calendar_window_is_exactly_365_calendar_days_and_exclusive_at_end(self):
        self.assertEqual(self.window.start_day, date(2025, 11, 4))
        self.assertEqual(self.window.end_day, date(2026, 11, 3))
        self.assertEqual(len(self.window.dates), 365)
        self.assertEqual(self.window.start.date(), self.window.start_day)
        self.assertEqual(self.window.end.date(), date(2026, 11, 4))
        self.assertEqual(self.window.end - self.window.start, timedelta(days=365))

    def test_window_uses_the_configured_timezone_for_today_and_midnight_bounds(self):
        local_zone = timezone(timedelta(hours=2))
        instant = datetime(2026, 11, 3, 22, 30, tzinfo=timezone.utc)
        with patch.object(module, "resolve_timezone", return_value=local_zone):
            window = module.contribution_window(now=instant, timezone_name="Test/Zone")
        self.assertEqual(window.end_day, date(2026, 11, 4))
        self.assertEqual(window.start_day, date(2025, 11, 5))
        self.assertEqual(window.start.utcoffset(), timedelta(hours=2))
        self.assertEqual(window.start.timetz().replace(tzinfo=None).isoformat(), "00:00:00")

    def test_monthly_aggregation_keeps_every_day_in_a_13_month_window(self):
        data = sample_data(self.window, lambda _day, index: index % 5)
        metrics = module.normalize(data, self.window, "salahsaeed19")
        self.assertEqual(list(metrics["months"]), [
            "2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05",
            "2026-06", "2026-07", "2026-08", "2026-09", "2026-10", "2026-11",
        ])
        self.assertEqual(sum(metrics["months"].values()), metrics["total"])
        self.assertEqual(metrics["months"]["2025-11"], sum(index % 5 for index in range(27)))
        self.assertEqual(metrics["months"]["2026-11"], sum(index % 5 for index in range(362, 365)))

    def test_calendar_rejects_missing_days_instead_of_silently_dropping_them(self):
        data = sample_data(self.window)
        data["user"]["contributionsCollection"]["contributionCalendar"]["weeks"][0]["contributionDays"].pop()
        with self.assertRaisesRegex(module.AnalyticsError, "incomplete"):
            module.normalize(data, self.window, "salahsaeed19")

    def test_calendar_rejects_total_mismatch(self):
        data = sample_data(self.window, lambda _day, _index: 1)
        data["user"]["contributionsCollection"]["contributionCalendar"]["totalContributions"] += 1
        with self.assertRaisesRegex(module.AnalyticsError, "does not match"):
            module.normalize(data, self.window, "salahsaeed19")

    def test_private_label_requires_owner_personal_token_and_read_user_scope(self):
        data = sample_data(self.window)
        public_metrics = module.normalize(data, self.window, "salahsaeed19")
        self.assertFalse(public_metrics["private_access_verified"])
        self.assertEqual(public_metrics["scope_label"], "Private inclusion unverified")
        owner_with_scope = module.AuthenticationContext("salahsaeed19", frozenset({"read:user"}), True)
        owner_without_scope = module.AuthenticationContext("salahsaeed19", frozenset(), True)
        self.assertFalse(module.normalize(data, self.window, "salahsaeed19", owner_with_scope, "workflow")["private_access_verified"])
        self.assertFalse(module.normalize(data, self.window, "salahsaeed19", owner_without_scope, "personal")["private_access_verified"])
        data["viewer"]["login"] = "another-user"
        self.assertFalse(module.normalize(data, self.window, "salahsaeed19", owner_with_scope, "personal")["private_access_verified"])
        data["viewer"]["login"] = "salahsaeed19"
        metrics = module.normalize(data, self.window, "salahsaeed19", owner_with_scope, "personal")
        self.assertTrue(metrics["private_access_verified"])
        self.assertEqual(metrics["scope_label"], "Public + eligible private activity")

    def test_rest_authentication_context_verifies_login_and_scope_header(self):
        class FakeResponse:
            headers = {"X-OAuth-Scopes": "read:user, user:email"}

            def read(self):
                return b'{"login": "salahsaeed19"}'

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with patch.object(module.urllib.request, "urlopen", return_value=FakeResponse()) as urlopen:
            auth = module.authenticated_context("not-a-real-token")
        self.assertEqual(auth.login, "salahsaeed19")
        self.assertTrue(auth.scopes_observed)
        self.assertIn("read:user", auth.oauth_scopes)
        self.assertEqual(urlopen.call_args.args[0].full_url, "https://api.github.com/user")

        with patch.object(module.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")):
            unavailable = module.authenticated_context("not-a-real-token")
        self.assertIsNone(unavailable.login)
        self.assertFalse(unavailable.scopes_observed)

    def test_failed_graphql_and_empty_calendar_are_safe_errors(self):
        with patch.object(module.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")):
            with self.assertRaisesRegex(module.AnalyticsError, "Could not reach"):
                module.graphql("not-a-real-token", "salahsaeed19", self.window)
        empty = {"viewer": {"login": "salahsaeed19"}, "user": {"contributionsCollection": {"contributionCalendar": {"totalContributions": 0, "weeks": []}}}}
        with self.assertRaisesRegex(module.AnalyticsError, "incomplete"):
            module.normalize(empty, self.window, "salahsaeed19")

    def test_svg_assets_have_expected_layout_and_valid_xml(self):
        metrics = module.normalize(sample_data(self.window, lambda _day, index: index % 3), self.window, "salahsaeed19")
        assets = (
            module.stats_svg(metrics),
            module.trend_svg(metrics),
            module.heatmap_svg(metrics),
            module.technology_svg(),
            module.stats_svg(None),
        )
        for source in assets:
            root = ET.fromstring(source)
            self.assertTrue(root.tag.endswith("svg"))
            self.assertEqual(root.attrib["viewBox"].split()[0:2], ["0", "0"])
        heatmap = ET.fromstring(module.heatmap_svg(metrics))
        tiles = [element for element in heatmap.iter() if element.tag.endswith("rect") and element.attrib.get("width") == "12"]
        self.assertEqual(len(tiles), 376)  # 53 x 7 calendar cells plus five legend cells
        self.assertIn(self.window.start_day.isoformat(), module.heatmap_svg(metrics))
        self.assertIn(self.window.end_day.isoformat(), module.heatmap_svg(metrics))
        trend = ET.fromstring(module.trend_svg(metrics))
        bars = [element for element in trend.iter() if element.tag.endswith("rect") and element.attrib.get("width") == "40"]
        self.assertEqual(len(bars), 13)

    def test_scope_parser_is_case_and_whitespace_tolerant(self):
        self.assertEqual(module.parse_oauth_scopes(" repo, READ:USER , user:email "), frozenset({"repo", "read:user", "user:email"}))


if __name__ == "__main__":
    unittest.main()
