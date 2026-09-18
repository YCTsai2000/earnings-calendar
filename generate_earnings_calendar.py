#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
自訂觀察名單財報行事曆產生器（Finnhub 版）
============================================================

功能：
1. 逐檔向 Finnhub 查詢 watchlist.txt 中的股票，避免全市場查詢遭截斷
2. Finnhub 為唯一財報資料來源，不再區分「官方確認」與「預估」
3. GitHub Actions 每天重新查詢，日期、EPS 與營收會依 Finnhub 最新資料更新
4. 財報公布後保留七天，等待 Finnhub 補入實際 EPS 與營收
5. 只有 Finnhub API 查詢失敗時，才暫時沿用上一版仍有效的事件
6. Finnhub 的 date 視為美國東部時間（America/New_York）的日期
7. BMO / AMC / DMH 使用概略公布時間
8. ICS 使用 America/New_York 時區儲存事件，並附上 VTIMEZONE
   （EST/EDT 日光節約時間規則），符合 RFC 5545 規範
9. Google Calendar / Apple Calendar / Outlook
   會依照使用者自己的行事曆時區自動轉換

時段概略時間：
    BMO = 07:00 America/New_York
    AMC = 16:30 America/New_York
    DMH = 13:00 America/New_York

注意：
Finnhub 的 hour 只有 bmo / amc / dmh，
沒有提供精確公布時間。

因此：
    07:00 / 16:30 / 13:00
只是為了建立正確的跨時區事件，
不是宣稱公司一定在該時間公布財報。

例如：

ORCL
美東：
    2026/09/10 16:30

台灣：
    2026/09/11 04:30

日本：
    2026/09/11 05:30

紐約：
    2026/09/10 16:30

行事曆會依照使用者自己的時區自動顯示。
"""

import argparse
import datetime
import os
import re
import sys
import time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:
    sys.exit(
        "找不到 requests，請先執行："
        "pip install requests --break-system-packages"
    )


# ============================================================
# Finnhub
# ============================================================

FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"


# ============================================================
# 美國東部時間
# ============================================================

# ICS 中所有財報事件的基準時區
US_TIMEZONE = "America/New_York"


# ============================================================
# Finnhub hour 對應的概略公布時間
# ============================================================

HOUR_LABEL = {
    "bmo": "盤前公布（美股開盤前）",
    "amc": "盤後公布（美股收盤後）",
    "dmh": "盤中公布",
}


HOUR_APPROX_TIME = {
    # Before Market Open
    "bmo": datetime.time(7, 0),

    # After Market Close
    "amc": datetime.time(16, 30),

    # During Market Hours
    "dmh": datetime.time(13, 0),
}


# hour 缺漏時的保底時間
DEFAULT_APPROX_TIME = datetime.time(12, 0)


# ============================================================
# 讀取觀察名單
# ============================================================

def load_watchlist(path: str) -> set:
    """
    讀取觀察名單檔案。

    格式：
        AAPL
        NVDA
        ORCL

    也可以：
        AAPL   # 蘋果
        NVDA   # Nvidia

    規則：
        - 一行一個股票代號
        - # 開頭整行視為註解
        - 行尾可以使用 # 加註解
        - 空白行忽略
        - 大小寫不敏感
    """

    if not os.path.exists(path):
        sys.exit(
            f"[錯誤] 找不到觀察名單檔案：{path}"
        )

    symbols = set()

    with open(path, "r", encoding="utf-8") as f:

        for line in f:

            line = line.strip()

            # 空白行
            if not line:
                continue

            # 整行註解
            if line.startswith("#"):
                continue

            # 去除行尾註解
            symbol = line.split("#")[0].strip()

            if symbol:
                symbols.add(symbol.upper())

    return symbols


# ============================================================
# Finnhub API
# ============================================================

def fetch_symbol_earnings(
    symbol: str,
    start_date: datetime.date,
    end_date: datetime.date,
    api_key: str,
    max_retries: int = 3,
):
    """
    只查詢一檔股票的財報資料。

    Finnhub 偶爾會回傳 429 或暫時性伺服器錯誤；這些情況會重試。
    回傳後仍再次核對 symbol 與日期範圍，避免 API 忽略篩選參數。
    """

    params = {
        "from": start_date.strftime("%Y-%m-%d"),
        "to": end_date.strftime("%Y-%m-%d"),
        "symbol": symbol,
        "token": api_key,
    }

    last_error = None

    for attempt in range(max_retries):
        resp = None

        try:
            resp = requests.get(
                FINNHUB_URL,
                params=params,
                timeout=30,
            )
        except requests.RequestException as e:
            last_error = f"連線失敗：{e}"
        else:
            if resp.status_code == 401:
                sys.exit(
                    "[錯誤] Finnhub 回傳 401："
                    "API 金鑰無效，請確認 FINNHUB_API_KEY 是否正確"
                )

            if resp.status_code == 403:
                sys.exit(
                    "[錯誤] Finnhub 回傳 403："
                    "這個端點可能受到方案限制，"
                    "請登入 Finnhub Dashboard 確認 API 權限"
                )

            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
            else:
                try:
                    resp.raise_for_status()
                    data = resp.json()
                except requests.RequestException as e:
                    raise RuntimeError(
                        f"{symbol} 查詢失敗：{e}"
                    ) from e
                except ValueError as e:
                    raise RuntimeError(
                        f"{symbol} 回傳內容不是有效 JSON"
                    ) from e

                items = []

                for item in data.get("earningsCalendar", []):
                    item_symbol = (
                        item.get("symbol", "").upper()
                    )
                    date_str = item.get("date", "")

                    try:
                        item_date = datetime.date.fromisoformat(date_str)
                    except ValueError:
                        continue

                    if (
                        item_symbol == symbol
                        and start_date <= item_date <= end_date
                    ):
                        items.append(item)

                return items

        if attempt < max_retries - 1:
            retry_after = 2 ** attempt
            if resp is not None:
                header_value = resp.headers.get("Retry-After")
                if header_value and header_value.isdigit():
                    retry_after = max(retry_after, int(header_value))
            time.sleep(retry_after)

    raise RuntimeError(
        f"{symbol} 查詢失敗（重試 {max_retries} 次）：{last_error}"
    )


def fetch_watchlist_earnings(
    watchlist: set,
    start_date: datetime.date,
    end_date: datetime.date,
    api_key: str,
    request_delay: float = 0.25,
):
    """
    逐檔查詢觀察名單。

    回傳 (results, errors)：
    results 的值為 list 代表查詢成功（空 list 表示 Finnhub 沒有資料），
    值為 None 代表該檔查詢發生暫時性錯誤。
    """

    results = {}
    errors = {}

    for index, symbol in enumerate(sorted(watchlist)):
        if index and request_delay > 0:
            time.sleep(request_delay)

        try:
            results[symbol] = fetch_symbol_earnings(
                symbol,
                start_date,
                end_date,
                api_key,
            )
        except RuntimeError as e:
            results[symbol] = None
            errors[symbol] = str(e)

    return results, errors


# ============================================================
# 讀取上一版 ICS（資料源暫時漏值時的保護）
# ============================================================

def load_existing_events(
    path: str,
    watchlist: set,
    start_date: datetime.date,
    end_date: datetime.date,
):
    """
    讀取上一版 ICS 中仍位於查詢區間的 watchlist 事件。

    保留原始 VEVENT 文字，僅在 API 查詢失敗時作為暫時備援。
    """

    if not os.path.exists(path):
        return []

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    blocks = re.findall(
        r"BEGIN:VEVENT\r?\n.*?\r?\nEND:VEVENT",
        content,
        flags=re.DOTALL,
    )
    events = []

    for block in blocks:
        unfolded = re.sub(r"\r?\n[ \t]", "", block)
        uid_match = re.search(
            r"^UID:earnings-(.+)-(\d{8})@earnings-calendar-script$",
            unfolded,
            flags=re.MULTILINE,
        )

        if not uid_match:
            continue

        symbol = uid_match.group(1).upper()

        try:
            event_date = datetime.datetime.strptime(
                uid_match.group(2),
                "%Y%m%d",
            ).date()
        except ValueError:
            continue

        if (
            symbol not in watchlist
            or event_date < start_date
            or event_date > end_date
        ):
            continue

        normalized_block = "\r\n".join(
            block.replace("\r\n", "\n").splitlines()
        )

        events.append({
            "symbol": symbol,
            "date": event_date,
            "raw": normalized_block,
        })

    return events


def merge_earnings_events(
    results: dict,
    existing_events: list,
    today: datetime.date = None,
):
    """
    Finnhub 是唯一資料來源。

    每次成功查詢都直接使用 Finnhub 最新結果，因此不需要維護
    「官方／預估」狀態。只有兩種情況會暫時保留上一版事件：

    1. 該股票本次 Finnhub 查詢失敗（值為 None）
    2. 財報已經發生且仍位於 days_behind 保留區間內，避免 Finnhub
       在補入實際 EPS / 營收前暫時漏掉事件

    尚未到公布日且 Finnhub 成功回傳新的資料或空資料時，
    上一版日期不再保留，讓行事曆跟著 Finnhub 最新資料更新。
    """

    if today is None:
        today = datetime.date.today()

    fresh_by_key = {}

    for symbol, items in results.items():
        if not items:
            continue

        for item in items:
            fresh_item = dict(item)
            fresh_item["_source_name"] = "Finnhub"
            key = (symbol, fresh_item.get("date", ""))
            fresh_by_key[key] = fresh_item

    preserved = []

    for event in existing_events:
        symbol = event["symbol"]
        event_key = (symbol, event["date"].isoformat())

        if event_key in fresh_by_key:
            continue

        api_items = results.get(symbol)

        if api_items is None or event["date"] <= today:
            preserved.append(event)

    fresh = sorted(fresh_by_key.values(), key=event_sort_key)
    preserved.sort(key=lambda event: (event["date"], event["symbol"]))

    return fresh, preserved


# ============================================================
# ICS 字串跳脫
# ============================================================

def ics_escape(text: str) -> str:
    """
    ICS DESCRIPTION / SUMMARY 使用的特殊字元跳脫。
    """

    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


# ============================================================
# ICS 行折疊
# ============================================================

def fold_line(line: str, limit: int = 75) -> str:
    """
    RFC 5545：
    ICS 每一行建議不要超過 75 octets。

    這裡按照 UTF-8 bytes 進行切割，
    避免中文被切斷。
    """

    line_bytes = line.encode("utf-8")

    if len(line_bytes) <= limit:
        return line

    chunks = []

    current = b""

    for ch in line:

        ch_bytes = ch.encode("utf-8")

        if len(current) + len(ch_bytes) > limit:

            chunks.append(current)

            current = b""

        current += ch_bytes

    if current:
        chunks.append(current)

    folded = chunks[0].decode("utf-8")

    for chunk in chunks[1:]:

        folded += (
            "\r\n "
            + chunk.decode("utf-8")
        )

    return folded


# ============================================================
# 數值格式
# ============================================================

def fmt_num(x):
    """
    None / 空字串 → 無資料
    """

    if x is None or x == "":
        return "無資料"

    return str(x)


def fmt_revenue(x):
    """
    營收以千分位顯示；None / 空字串 → 無資料。
    """

    if x is None or x == "":
        return "無資料"

    try:
        value = Decimal(str(x))
    except (InvalidOperation, ValueError):
        return str(x)

    if not value.is_finite():
        return str(x)

    formatted = format(value, ",f")

    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")

    return formatted


# ============================================================
# 建立美東時間
# ============================================================

def get_us_eastern_datetime(item: dict) -> datetime.datetime:
    """
    將 Finnhub：

        date = 2026-09-10
        hour = amc

    轉成：

        2026-09-10 16:30
        America/New_York

    注意：
    這裡不是在判斷使用者所在地。

    我們只建立「財報發生的基準時間」，
    後續交由行事曆軟體自行轉換。
    """

    us_date = datetime.datetime.strptime(
        item["date"],
        "%Y-%m-%d"
    ).date()

    hour_code = (
        item.get("hour", "")
        or ""
    ).lower()

    exact_time = str(item.get("time", "")).strip()

    if exact_time:
        event_time = datetime.time.fromisoformat(exact_time)
    else:
        event_time = HOUR_APPROX_TIME.get(
            hour_code,
            DEFAULT_APPROX_TIME
        )

    # Python 的 zoneinfo / ICS 使用 IANA 時區
    # 美國東部：
    # America/New_York
    us_tz = ZoneInfo(US_TIMEZONE)

    us_dt = datetime.datetime.combine(
        us_date,
        event_time
    ).replace(tzinfo=us_tz)

    return us_dt


# ============================================================
# ICS 時間格式
# ============================================================

def format_ics_local_datetime(dt: datetime.datetime) -> str:
    """
    將 datetime 格式化成 ICS：

        YYYYMMDDTHHMMSS
    """

    return dt.strftime(
        "%Y%m%dT%H%M%S"
    )


# ============================================================
# 建立單一財報事件
# ============================================================

def build_event(
    item: dict,
    now_stamp: str,
    alarm_hours_before: int
) -> str:

    symbol = (
        item.get("symbol", "")
        .upper()
    )

    # --------------------------------------------------------
    # 美國東部時間
    # --------------------------------------------------------

    us_dt = get_us_eastern_datetime(item)

    us_date = us_dt.date()

    # --------------------------------------------------------
    # ICS DTSTART
    #
    # 使用具名時區：
    #
    # DTSTART;TZID=America/New_York:20260910T163000
    #
    # 這樣 Calendar 軟體可以自行轉換時區。
    # 對應的 VTIMEZONE 定義在 VCALENDAR 開頭。
    # --------------------------------------------------------

    dtstart = format_ics_local_datetime(
        us_dt
    )

    # --------------------------------------------------------
    # 財報季度
    # --------------------------------------------------------

    quarter = item.get(
        "quarter",
        "?"
    )

    year = item.get(
        "year",
        ""
    )

    source_name = item.get(
        "_source_name",
        "Finnhub",
    )

    summary = (
        f"{symbol} 財報 "
        f"(Q{quarter} {year})"
    )

    # --------------------------------------------------------
    # DESCRIPTION
    # --------------------------------------------------------

    time_line = (
        "概略美東時間："
        + f"{us_dt.strftime('%Y-%m-%d %H:%M')} "
        f"({US_TIMEZONE})"
    )

    description_lines = [
        f"股票代號：{symbol}",
        f"資料來源：{source_name}",
        time_line,
        "注意：Finnhub 資料每日自動更新，日期與時間可能變動。",
        f"財測 EPS："
        f"{fmt_num(item.get('epsEstimate'))}",
        f"實際 EPS："
        f"{fmt_num(item.get('epsActual'))}",
        f"財測營收："
        f"{fmt_revenue(item.get('revenueEstimate'))}",
        f"實際營收："
        f"{fmt_revenue(item.get('revenueActual'))}",
    ]

    description = "\n".join(
        description_lines
    )

    has_actual_results = any(
        item.get(field) not in (None, "")
        for field in ("epsActual", "revenueActual")
    )
    sequence = 1 if has_actual_results else 0

    # --------------------------------------------------------
    # UID
    #
    # 使用「股票 + 美國財報日期」
    # 保持事件 ID 穩定。
    #
    # GitHub Actions 每天重新產生 ICS，
    # 不會因為 DTSTAMP 改變而建立新事件。
    # --------------------------------------------------------

    uid = (
        f"earnings-"
        f"{symbol}-"
        f"{us_date.strftime('%Y%m%d')}"
        f"@earnings-calendar-script"
    )

    # --------------------------------------------------------
    # VEVENT
    # --------------------------------------------------------

    lines = [

        "BEGIN:VEVENT",

        # ★ 核心：
        # 有時間的 America/New_York 事件
        f"DTSTART;TZID={US_TIMEZONE}:"
        f"{dtstart}",

        f"DTSTAMP:{now_stamp}",

        f"UID:{uid}",

        f"CREATED:{now_stamp}",

        f"DESCRIPTION:"
        f"{ics_escape(description)}",

        f"LAST-MODIFIED:{now_stamp}",

        f"SEQUENCE:{sequence}",

        f"SUMMARY:"
        f"{ics_escape(summary)}",

        "TRANSP:OPAQUE",

        # ----------------------------------------------------
        # 提醒
        # ----------------------------------------------------

        "BEGIN:VALARM",

        "ACTION:DISPLAY",

        f"DESCRIPTION:"
        f"{ics_escape(summary)}",

        f"TRIGGER:-PT"
        f"{alarm_hours_before}H",

        "END:VALARM",

        "END:VEVENT",
    ]

    return "\r\n".join(
        fold_line(line)
        for line in lines
    )


# ============================================================
# 排序
# ============================================================

def event_sort_key(item: dict):

    date_str = item.get(
        "date",
        "9999-12-31"
    )

    symbol = (
        item.get("symbol", "")
        .upper()
    )

    return (
        date_str,
        symbol
    )


# ============================================================
# 查詢日期範圍
# ============================================================

def get_query_date_range(
    today: datetime.date,
    days_behind: int,
    days_ahead: int,
):
    """
    財報公布後仍保留 days_behind 天，並查詢未來 days_ahead 天。
    """

    return (
        today - datetime.timedelta(days=days_behind),
        today + datetime.timedelta(days=days_ahead),
    )


# ============================================================
# 主程式
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "產生自訂觀察名單的財報 "
            ".ics 行事曆檔（Finnhub 版）"
        )
    )

    parser.add_argument(
        "--watchlist",
        type=str,
        default="watchlist.txt",
        help=(
            "觀察名單檔案路徑"
        ),
    )

    parser.add_argument(
        "--days-ahead",
        type=int,
        default=45,
        help=(
            "從今天起往後抓幾天的財報"
        ),
    )

    parser.add_argument(
        "--days-behind",
        type=int,
        default=7,
        help=(
            "財報公布後保留幾天"
            "（預設 7 天）"
        ),
    )

    parser.add_argument(
        "--output",
        type=str,
        default="docs/earnings.ics",
        help=(
            "輸出檔案路徑"
        ),
    )

    parser.add_argument(
        "--alarm-hours-before",
        type=int,
        default=28,
        help=(
            "提醒設在事件前幾小時"
            "（預設 28 小時）"
        ),
    )

    args = parser.parse_args()

    # ========================================================
    # 檢查參數
    # ========================================================

    if args.days_ahead < 0:

        sys.exit(
            "[錯誤] --days-ahead 不可以小於 0"
        )

    if args.days_behind < 0:

        sys.exit(
            "[錯誤] --days-behind 不可以小於 0"
        )

    if args.alarm_hours_before < 0:

        sys.exit(
            "[錯誤] --alarm-hours-before "
            "不可以小於 0"
        )

    # ========================================================
    # API Key
    # ========================================================

    api_key = os.environ.get(
        "FINNHUB_API_KEY"
    )

    if not api_key:

        sys.exit(
            "[錯誤] 找不到環境變數 "
            "FINNHUB_API_KEY，"
            "請先設定好金鑰"
        )

    # ========================================================
    # 讀取觀察名單
    # ========================================================

    watchlist = load_watchlist(
        args.watchlist
    )

    if not watchlist:

        sys.exit(
            "[錯誤] 觀察名單是空的"
        )

    print(
        f"觀察名單共 "
        f"{len(watchlist)} 檔："
        f"{', '.join(sorted(watchlist))}"
    )

    # ========================================================
    # 日期範圍
    # ========================================================

    # GitHub Actions 於 UTC 00:00 執行；沿用 UTC 日曆日作為
    # 45 天前瞻區間的基準，避免台灣早上更新時少抓最遠端一天。
    today = datetime.date.today()

    start_date, end_date = get_query_date_range(
        today,
        args.days_behind,
        args.days_ahead,
    )

    print(
        f"正在逐檔向 Finnhub 查詢 "
        f"{start_date} 到 {end_date} "
        f"的財報資料..."
    )

    # ========================================================
    # API
    # ========================================================

    existing_events = load_existing_events(
        args.output,
        watchlist,
        start_date,
        end_date,
    )

    results, query_errors = fetch_watchlist_earnings(
        watchlist,
        start_date,
        end_date,
        api_key,
    )

    events, preserved_events = merge_earnings_events(
        results,
        existing_events,
        today,
    )

    print(
        f"逐檔查詢完成：Finnhub 事件 {len(events)} 筆，"
        f"API 失敗暫存 {len(preserved_events)} 筆"
    )

    existing_by_symbol = {}
    for event in preserved_events:
        existing_by_symbol.setdefault(event["symbol"], []).append(event)

    for symbol in sorted(watchlist):
        items = results[symbol]

        if items:
            dates = ", ".join(item["date"] for item in items)
            print(f"[FINNHUB] {symbol:<6} {dates}")
            continue

        preserved_for_symbol = existing_by_symbol.get(symbol, [])
        if preserved_for_symbol:
            dates = ", ".join(
                event["date"].isoformat()
                for event in preserved_for_symbol
            )
            reason = query_errors.get(
                symbol,
                "Finnhub 查詢失敗",
            )
            message = (
                f"{symbol} {reason}；"
                f"暫時沿用上一版事件：{dates}"
            )
        elif items is None:
            message = (
                f"{query_errors[symbol]}，"
                "且沒有可保留的既有事件"
            )
        else:
            message = f"{symbol} 找不到查詢區間內的財報日期"

        print(f"[WARN] {message}")
        print(f"::warning::{message}")

    # ========================================================
    # 顯示事件
    # ========================================================

    print()
    print(
        "財報事件（美東時間）："
    )
    print(
        "------------------------------------------------------------"
    )

    for item in events:

        symbol = (
            item.get("symbol", "")
            .upper()
        )

        try:

            us_dt = get_us_eastern_datetime(
                item
            )

            hour_code = (
                item.get("hour", "")
                or ""
            ).lower()

            hour_label = HOUR_LABEL.get(
                hour_code,
                "時間未公布"
            )

            print(
                f"{us_dt.strftime('%Y-%m-%d %H:%M')} "
                f"ET | "
                f"{symbol:<6} | "
                f"{hour_label}"
            )

        except Exception as e:

            print(
                f"[警告] "
                f"{symbol} 日期處理失敗：{e}"
            )

    print(
        "------------------------------------------------------------"
    )

    for event in preserved_events:
        print(
            f"{event['date'].isoformat()} --:-- ET | "
            f"{event['symbol']:<6} | API 失敗，暫用上一版事件"
        )

    if preserved_events:
        print(
            "------------------------------------------------------------"
        )

    # ========================================================
    # DTSTAMP
    # ========================================================

    now_stamp = (
        datetime.datetime
        .now(datetime.timezone.utc)
        .strftime(
            "%Y%m%dT%H%M%SZ"
        )
    )

    # ========================================================
    # VCALENDAR
    # ========================================================

    calendar_lines = [

        "BEGIN:VCALENDAR",

        "PRODID:"
        "//EarningsCalendarScript"
        "//Watchlist 4.0"
        "//EN",

        "VERSION:2.0",

        "CALSCALE:GREGORIAN",

        "METHOD:PUBLISH",

        "X-WR-CALNAME:"
        "我的觀察名單財報行事曆"
        "（自動更新）",

        # ★ 行事曆的基準時區
        #
        # 注意：
        # 這不是把事件鎖死在台灣。
        #
        # 真正的事件 DTSTART 仍然使用：
        # America/New_York
        #
        # 使用者的 Calendar App 會依自己的
        # 時區設定進行顯示。
        "X-WR-TIMEZONE:"
        "America/New_York",

        "REFRESH-INTERVAL;"
        "VALUE=DURATION:P1D",

        "X-PUBLISHED-TTL:P1D",

        # --------------------------------------------------
        # VTIMEZONE：America/New_York
        #
        # RFC 5545 規定，事件裡如果用具名時區
        # （DTSTART;TZID=America/New_York:...），
        # 檔案裡最好附上對應的 VTIMEZONE 定義，
        # 說明 EST/EDT 的日光節約時間規則。
        #
        # 沒有這段，多數現代行事曆（Google/Apple/
        # Outlook）仍能靠時區名稱自己認得，
        # 但比較嚴謹的解析器可能會把事件當成
        # 「浮動時間」處理，換算就不準了。
        # 補上這段才是符合規範、最保險的做法。
        #
        # 規則採用美國自 2007 年起的標準：
        # 3 月第二個週日轉 EDT，
        # 11 月第一個週日轉回 EST。
        # --------------------------------------------------
        "BEGIN:VTIMEZONE",
        "TZID:America/New_York",
        "X-LIC-LOCATION:America/New_York",
        "BEGIN:DAYLIGHT",
        "TZOFFSETFROM:-0500",
        "TZOFFSETTO:-0400",
        "TZNAME:EDT",
        "DTSTART:19700308T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
        "END:DAYLIGHT",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:-0400",
        "TZOFFSETTO:-0500",
        "TZNAME:EST",
        "DTSTART:19701101T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU",
        "END:STANDARD",
        "END:VTIMEZONE",

    ]

    body = "\r\n".join(
        fold_line(line)
        for line in calendar_lines
    )

    # ========================================================
    # 加入 VEVENT
    # ========================================================

    rendered_events = []

    for item in events:

        rendered_events.append(
            build_event(
                item,
                now_stamp,
                args.alarm_hours_before
            )
        )

    rendered_events.extend(
        event["raw"]
        for event in preserved_events
    )

    if rendered_events:
        body += "\r\n" + "\r\n".join(rendered_events)

    # ========================================================
    # 結束 VCALENDAR
    # ========================================================

    body += (
        "\r\nEND:VCALENDAR\r\n"
    )

    # ========================================================
    # 建立輸出資料夾
    # ========================================================

    out_dir = os.path.dirname(
        args.output
    )

    if out_dir:

        os.makedirs(
            out_dir,
            exist_ok=True
        )

    # ========================================================
    # 寫入 ICS
    # ========================================================

    with open(
        args.output,
        "w",
        encoding="utf-8",
        newline=""
    ) as f:

        f.write(body)

    # ========================================================
    # 完成
    # ========================================================

    print()
    print(
        f"已產生 "
        f"{len(rendered_events)} 個事件"
        f" -> {args.output}"
    )

    print()
    print(
        "ICS 時區設定："
        f"{US_TIMEZONE}"
    )

    print(
        "行事曆將依使用者自己的時區"
        "自動轉換事件時間。"
    )


# ============================================================
# Entry Point
# ============================================================

if __name__ == "__main__":
    main()
