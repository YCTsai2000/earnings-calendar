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


class DateRangeTests(unittest.TestCase):
    def test_query_range_keeps_previous_seven_days(self):
        start, end = calendar.get_query_date_range(START, 7, 45)

        self.assertEqual(start, datetime.date(2026, 9, 11))
        self.assertEqual(end, datetime.date(2026, 11, 2))


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


class OfficialAnnouncementDetectionTests(unittest.TestCase):
    def setUp(self):
        self.api_item = {
            "symbol": "MU",
            "date": "2026-09-30",
            "hour": "amc",
            "quarter": 4,
            "year": 2026,
            "epsEstimate": 32.2164,
        }

    def test_company_press_release_promotes_matching_candidate(self):
        news = [{
            "headline": (
                "Micron Technology to Report Fiscal Fourth Quarter "
                "Results on September 30, 2026"
            ),
            "summary": (
                "Micron today announced that it will report results "
                "after market close on September 30, 2026."
            ),
            "source": "GlobeNewswire",
            "url": "https://www.globenewswire.com/example",
        }]

        official = calendar.find_official_confirmations_for_symbol(
            "MU",
            [self.api_item],
            news,
        )

        self.assertEqual(len(official), 1)
        self.assertEqual(
            official[0]["_status"],
            calendar.STATUS_OFFICIAL,
        )
        self.assertEqual(official[0]["_source_name"], "GlobeNewswire")
        self.assertEqual(official[0]["hour"], "amc")
        self.assertNotIn("time", official[0])

    def test_media_speculation_is_not_promoted(self):
        news = [{
            "headline": (
                "Micron to report fiscal fourth quarter results "
                "on September 30, 2026"
            ),
            "summary": (
                "Analysts expect Micron to report on "
                "September 30, 2026."
            ),
            "source": "Reuters",
            "url": "https://www.reuters.com/example",
        }]

        official = calendar.find_official_confirmations_for_symbol(
            "MU",
            [self.api_item],
            news,
        )

        self.assertEqual(official, [])

    def test_mismatched_announcement_date_is_not_promoted(self):
        news = [{
            "headline": (
                "Micron Technology to Report Fiscal Fourth Quarter "
                "Results on October 1, 2026"
            ),
            "summary": (
                "Micron today announced that it will report results "
                "after market close on October 1, 2026."
            ),
            "source": "Business Wire",
            "url": "https://www.businesswire.com/example",
        }]

        official = calendar.find_official_confirmations_for_symbol(
            "MU",
            [self.api_item],
            news,
        )

        self.assertEqual(official, [])

    @patch.object(calendar.requests, "get")
    def test_company_news_query_uses_lookback_window(self, get):
        get.return_value = response(
            200,
            [{
                "headline": (
                    "Micron Technology to Report Fiscal Fourth Quarter "
                    "Results on September 30, 2026"
                )
            }],
        )

        items = calendar.fetch_symbol_company_news(
            "MU",
            START,
            "test-key",
        )

        self.assertEqual(len(items), 1)
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["symbol"], "MU")
        self.assertEqual(params["to"], "2026-09-18")
        self.assertEqual(params["from"], "2026-05-21")


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
            "DTSTART;TZID=America/New_York:20260912T163000\r\n"
            "UID:earnings-ORCL-20260912@earnings-calendar-script\r\n"
            "SUMMARY:ORCL 財報 (Q1 2027)\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_api_error_temporarily_preserves_existing_event(self):
        existing = calendar.load_existing_events(
            str(self.ics_path), {"MU", "ORCL"}, START, END
        )
        fresh, preserved = calendar.merge_earnings_events(
            {"MU": None, "ORCL": []}, [], existing, START
        )

        self.assertEqual(fresh, [])
        self.assertEqual([event["symbol"] for event in preserved], ["MU"])
        self.assertIn("UID:earnings-MU-20260930", preserved[0]["raw"])

    def test_successful_empty_response_drops_old_estimate(self):
        existing = calendar.load_existing_events(
            str(self.ics_path), {"MU"}, START, END
        )

        fresh, preserved = calendar.merge_earnings_events(
            {"MU": []}, [], existing, START
        )

        self.assertEqual(fresh, [])
        self.assertEqual(preserved, [])

    def test_successful_empty_response_keeps_recent_past_event(self):
        start = START - datetime.timedelta(days=7)
        existing = calendar.load_existing_events(
            str(self.ics_path), {"ORCL"}, start, END
        )

        fresh, preserved = calendar.merge_earnings_events(
            {"ORCL": []}, [], existing, START
        )

        self.assertEqual(fresh, [])
        self.assertEqual(
            [event["symbol"] for event in preserved],
            ["ORCL"],
        )

    def _write_verified_official_mu(self):
        self.ics_path.write_text(
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\n"
            "DTSTART;TZID=America/New_York:20260930T163000\r\n"
            "UID:earnings-MU-20260930@earnings-calendar-script\r\n"
            "DESCRIPTION:股票代號：MU\\n"
            "資料來源：Micron Investor Relations\\n"
            "概略美東時間：2026-09-30 16:30 "
            "(America/New_York)\r\n"
            "X-EARNINGS-SOURCE-STATUS:OFFICIAL\r\n"
            "SUMMARY:MU 財報 (Q4 2026)\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n",
            encoding="utf-8",
        )

    def test_reads_official_status_and_source_from_existing_ics(self):
        self._write_verified_official_mu()

        existing = calendar.load_existing_events(
            str(self.ics_path), {"MU"}, START, END
        )

        self.assertEqual(existing[0]["status"], calendar.STATUS_OFFICIAL)
        self.assertEqual(
            existing[0]["source_name"],
            "Micron Investor Relations",
        )

    def test_matching_candidate_keeps_verified_official_status(self):
        self._write_verified_official_mu()
        existing = calendar.load_existing_events(
            str(self.ics_path), {"MU"}, START, END
        )
        api_item = {
            "symbol": "MU",
            "date": "2026-09-30",
            "hour": "amc",
            "quarter": 4,
            "year": 2026,
            "epsEstimate": 32.2164,
        }

        fresh, preserved = calendar.merge_earnings_events(
            {"MU": [api_item]},
            [],
            existing,
            START,
        )

        self.assertEqual(len(fresh), 1)
        self.assertEqual(
            fresh[0]["_status"],
            calendar.STATUS_OFFICIAL,
        )
        self.assertEqual(
            fresh[0]["_source_name"],
            "Micron Investor Relations",
        )
        self.assertEqual(preserved, [])

    def test_old_official_survives_third_party_date_movement(self):
        self._write_verified_official_mu()
        existing = calendar.load_existing_events(
            str(self.ics_path), {"MU"}, START, END
        )
        moved_estimate = {
            "symbol": "MU",
            "date": "2026-10-01",
            "hour": "amc",
            "quarter": 4,
            "year": 2026,
        }

        fresh, preserved = calendar.merge_earnings_events(
            {"MU": [moved_estimate]},
            [],
            existing,
            START,
        )

        self.assertEqual(
            fresh[0]["_status"],
            calendar.STATUS_ESTIMATED,
        )
        self.assertEqual(
            [event["date"].isoformat() for event in preserved],
            ["2026-09-30"],
        )

    def test_official_event_survives_missing_finnhub_row(self):
        existing = calendar.load_existing_events(
            str(self.ics_path), {"MU"}, START, END
        )
        official = {
            "symbol": "MU",
            "date": "2026-09-30",
            "time": "16:30",
            "hour": "amc",
            "quarter": 4,
            "year": 2026,
            "_status": calendar.STATUS_OFFICIAL,
            "_source_name": "Micron Investor Relations",
            "_source_url": "https://example.com/mu-official",
        }

        fresh, preserved = calendar.merge_earnings_events(
            {"MU": []}, [official], existing, START
        )

        self.assertEqual(fresh, [official])
        self.assertEqual(preserved, [])

    def test_official_event_keeps_finnhub_actual_results(self):
        official = {
            "symbol": "MU",
            "date": "2026-09-30",
            "time": "16:30",
            "hour": "amc",
            "quarter": 4,
            "year": 2026,
            "_status": calendar.STATUS_OFFICIAL,
        }
        api_item = {
            "symbol": "MU",
            "date": "2026-09-30",
            "epsActual": 4.56,
            "revenueActual": 12345678901,
        }

        fresh, preserved = calendar.merge_earnings_events(
            {"MU": [api_item]}, [official], [], START
        )

        self.assertEqual(fresh[0]["_status"], calendar.STATUS_OFFICIAL)
        self.assertEqual(fresh[0]["epsActual"], 4.56)
        self.assertEqual(fresh[0]["revenueActual"], 12345678901)
        self.assertEqual(preserved, [])


class SourceStatusTests(unittest.TestCase):
    def test_ics_distinguishes_official_and_estimated_events(self):
        official = calendar.build_event(
            {
                "symbol": "MU",
                "date": "2026-09-30",
                "time": "16:30",
                "hour": "amc",
                "quarter": 4,
                "year": 2026,
                "_status": calendar.STATUS_OFFICIAL,
                "_source_name": "Micron Investor Relations",
                "_source_url": "https://example.com/mu",
            },
            "20260918T000000Z",
            28,
        )
        estimated = calendar.build_event(
            {
                "symbol": "TSLA",
                "date": "2026-10-20",
                "hour": "amc",
                "quarter": 3,
                "year": 2026,
                "epsEstimate": 0.4508,
                "epsActual": None,
                "revenueEstimate": 28265984061,
                "revenueActual": None,
                "_status": calendar.STATUS_ESTIMATED,
                "_source_name": "Finnhub",
            },
            "20260918T000000Z",
            28,
        )
        official_unfolded = official.replace("\r\n ", "")
        estimated_unfolded = estimated.replace("\r\n ", "")

        self.assertIn(
            "SUMMARY:MU 財報 (Q4 2026)",
            official_unfolded,
        )
        self.assertNotIn("【官方確認】", official_unfolded)
        self.assertNotIn("【預估】", official_unfolded)
        self.assertIn("STATUS:CONFIRMED", official)
        self.assertIn("X-EARNINGS-SOURCE-STATUS:OFFICIAL", official)
        self.assertIn(
            "資料來源：Micron Investor Relations", official_unfolded
        )
        self.assertNotIn("官方來源：", official_unfolded)
        self.assertNotIn("URL:https://example.com/mu", official_unfolded)
        self.assertNotIn("資料狀態：", official_unfolded)
        self.assertNotIn("美東日期：", official_unfolded)
        self.assertNotIn("公布時段：", official_unfolded)

        official_description = next(
            line
            for line in official_unfolded.split("\r\n")
            if line.startswith("DESCRIPTION:")
        )
        self.assertEqual(
            official_description,
            "DESCRIPTION:股票代號：MU\\n"
            "資料來源：Micron Investor Relations\\n"
            "美東時間：2026-09-30 16:30 (America/New_York)\\n"
            "注意：日期與時間已由公司官方公告確認。\\n"
            "財測 EPS：無資料\\n"
            "實際 EPS：無資料\\n"
            "財測營收：無資料\\n"
            "實際營收：無資料",
        )
        self.assertIn(
            "SUMMARY:TSLA 財報 (Q3 2026)【預估】",
            estimated_unfolded,
        )
        self.assertNotIn("【第三方預估】", estimated_unfolded)
        self.assertIn("STATUS:TENTATIVE", estimated)
        self.assertIn("X-EARNINGS-SOURCE-STATUS:ESTIMATED", estimated)
        self.assertIn("資料來源：Finnhub", estimated_unfolded)
        self.assertNotIn("資料狀態：", estimated_unfolded)
        self.assertNotIn("美東日期：", estimated_unfolded)
        self.assertNotIn("公布時段：", estimated_unfolded)

        estimated_description = next(
            line
            for line in estimated_unfolded.split("\r\n")
            if line.startswith("DESCRIPTION:")
        )
        self.assertEqual(
            estimated_description,
            "DESCRIPTION:股票代號：TSLA\\n"
            "資料來源：Finnhub\\n"
            "概略美東時間：2026-10-20 16:30 "
            "(America/New_York)\\n"
            "注意：日期與時間尚未獲公司官方確認，可能變動。\\n"
            "財測 EPS：0.4508\\n"
            "實際 EPS：無資料\\n"
            "財測營收：28\\,265\\,984\\,061\\n"
            "實際營收：無資料",
        )

    def test_actual_results_raise_event_sequence(self):
        event = calendar.build_event(
            {
                "symbol": "TSLA",
                "date": "2026-10-20",
                "hour": "amc",
                "quarter": 3,
                "year": 2026,
                "epsActual": 0.52,
                "revenueActual": 28265984061,
                "_status": calendar.STATUS_ESTIMATED,
                "_source_name": "Finnhub",
            },
            "20261021T000000Z",
            28,
        ).replace("\r\n ", "")

        self.assertIn("SEQUENCE:1", event)
        self.assertIn("實際 EPS：0.52", event)
        self.assertIn("實際營收：28\\,265\\,984\\,061", event)

    def test_official_date_without_exact_time_marks_time_as_approximate(self):
        event = calendar.build_event(
            {
                "symbol": "MU",
                "date": "2026-09-30",
                "hour": "amc",
                "quarter": 4,
                "year": 2026,
                "_status": calendar.STATUS_OFFICIAL,
                "_source_name": "Micron Investor Relations",
            },
            "20260918T000000Z",
            28,
        ).replace("\r\n ", "")

        self.assertIn("概略美東時間：", event)
        self.assertIn(
            "注意：日期已由公司官方公告確認；時間為概略值。",
            event,
        )


if __name__ == "__main__":
    unittest.main()
