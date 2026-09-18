import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import generate_earnings_calendar as calendar


START = datetime.date(2026, 9, 18)
END = datetime.date(2026, 11, 2)


def response(status_code, payload=None, headers=None):
    result = Mock()
    result.status_code = status_code
    result.headers = headers or {}
    result.json.return_value = payload or {}

    if status_code >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(
            f"HTTP {status_code}"
        )
    else:
        result.raise_for_status.return_value = None

    return result


class FetchSymbolEarningsTests(unittest.TestCase):
    @patch.object(calendar.requests, "get")
    def test_queries_one_symbol_and_filters_unrelated_rows(self, get):
        get.return_value = response(
            200,
            {
                "earningsCalendar": [
                    {"symbol": "MU", "date": "2026-09-30"},
                    {"symbol": "AAPL", "date": "2026-10-28"},
                    {"symbol": "MU", "date": "2026-12-20"},
                ]
            },
        )

        items = calendar.fetch_symbol_earnings(
            "MU", START, END, "test-key"
        )

        self.assertEqual(items, [{"symbol": "MU", "date": "2026-09-30"}])
        self.assertEqual(get.call_args.kwargs["params"]["symbol"], "MU")

    @patch.object(calendar.time, "sleep")
    @patch.object(calendar.requests, "get")
    def test_retries_rate_limit(self, get, sleep):
        get.side_effect = [
            response(429, headers={"Retry-After": "1"}),
            response(
                200,
                {"earningsCalendar": [{"symbol": "MU", "date": "2026-09-30"}]},
            ),
        ]

        items = calendar.fetch_symbol_earnings(
            "MU", START, END, "test-key"
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(1)


class ExistingCalendarFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ics_path = Path(self.temp_dir.name) / "earnings.ics"
        self.ics_path.write_text(
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\n"
            "DTSTART;TZID=America/New_York:20260930T163000\r\n"
            "UID:earnings-MU-20260930@earnings-calendar-script\r\n"
            "SUMMARY:MU 財報 (Q4 2026)\r\n"
            "END:VEVENT\r\n"
            "BEGIN:VEVENT\r\n"
            "DTSTART;TZID=America/New_York:20260910T163000\r\n"
            "UID:earnings-ORCL-20260910@earnings-calendar-script\r\n"
            "SUMMARY:ORCL 財報 (Q1 2027)\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_missing_api_row_preserves_future_existing_event(self):
        existing = calendar.load_existing_events(
            str(self.ics_path), {"MU", "ORCL"}, START, END
        )
        fresh, preserved = calendar.merge_with_existing_events(
            {"MU": [], "ORCL": []}, existing
        )

        self.assertEqual(fresh, [])
        self.assertEqual([event["symbol"] for event in preserved], ["MU"])
        self.assertIn("UID:earnings-MU-20260930", preserved[0]["raw"])

    def test_fresh_row_replaces_existing_event_for_symbol(self):
        existing = calendar.load_existing_events(
            str(self.ics_path), {"MU"}, START, END
        )
        new_item = {"symbol": "MU", "date": "2026-10-01"}

        fresh, preserved = calendar.merge_with_existing_events(
            {"MU": [new_item]}, existing
        )

        self.assertEqual(fresh, [new_item])
        self.assertEqual(preserved, [])


if __name__ == "__main__":
    unittest.main()
