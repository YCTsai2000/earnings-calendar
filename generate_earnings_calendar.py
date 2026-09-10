#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
自訂觀察名單財報行事曆產生器（Finnhub 版）
============================================================

功能：
1. 從 Finnhub 取得未來指定天數的財報資料
2. 只保留 watchlist.txt 中的股票
3. Finnhub 的 date 視為美國東部時間（America/New_York）的日期
4. BMO / AMC / DMH 使用概略公布時間
5. ICS 使用 America/New_York 時區儲存事件，並附上 VTIMEZONE
   （EST/EDT 日光節約時間規則），符合 RFC 5545 規範
6. Google Calendar / Apple Calendar / Outlook
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
import sys

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

def fetch_earnings(
    start_date: datetime.date,
    end_date: datetime.date,
    api_key: str
):
    """
    一次呼叫 Finnhub，
    取得指定日期範圍的財報資料。
    """

    params = {
        "from": start_date.strftime("%Y-%m-%d"),
        "to": end_date.strftime("%Y-%m-%d"),
        "token": api_key,
    }

    try:

        resp = requests.get(
            FINNHUB_URL,
            params=params,
            timeout=30
        )

    except requests.RequestException as e:

        sys.exit(
            f"[錯誤] 無法連線到 Finnhub：{e}"
        )

    # API Key 錯誤
    if resp.status_code == 401:

        sys.exit(
            "[錯誤] Finnhub 回傳 401："
            "API 金鑰無效，請確認 FINNHUB_API_KEY 是否正確"
        )

    # 權限問題
    if resp.status_code == 403:

        sys.exit(
            "[錯誤] Finnhub 回傳 403："
            "這個端點可能受到方案限制，"
            "請登入 Finnhub Dashboard 確認 API 權限"
        )

    # 其他 HTTP 錯誤
    resp.raise_for_status()

    try:

        data = resp.json()

    except ValueError:

        sys.exit(
            "[錯誤] Finnhub 回傳內容不是有效 JSON"
        )

    return data.get("earningsCalendar", [])


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

    approx_time = HOUR_APPROX_TIME.get(
        hour_code,
        DEFAULT_APPROX_TIME
    )

    # Python 的 zoneinfo / ICS 使用 IANA 時區
    # 美國東部：
    # America/New_York
    from zoneinfo import ZoneInfo

    us_tz = ZoneInfo(US_TIMEZONE)

    us_dt = datetime.datetime.combine(
        us_date,
        approx_time
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

    hour_code = (
        item.get("hour", "")
        or ""
    ).lower()

    hour_label = HOUR_LABEL.get(
        hour_code,
        "時間未公布"
    )

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

    summary = (
        f"{symbol} 財報 "
        f"(Q{quarter} {year})"
    )

    # --------------------------------------------------------
    # DESCRIPTION
    # --------------------------------------------------------

    description_lines = [

        f"股票代號：{symbol}",

        f"美東日期："
        f"{us_date.isoformat()}",

        f"公布時段："
        f"{hour_label}",

        "注意："
        "公布時間為概略估計，"
        "僅用於跨時區日期換算。",

        f"概略美東時間："
        f"{us_dt.strftime('%Y-%m-%d %H:%M')} "
        f"({US_TIMEZONE})",

        f"財測 EPS："
        f"{fmt_num(item.get('epsEstimate'))}",

        f"實際 EPS："
        f"{fmt_num(item.get('epsActual'))}",

        f"財測營收："
        f"{fmt_num(item.get('revenueEstimate'))}",

        f"實際營收："
        f"{fmt_num(item.get('revenueActual'))}",
    ]

    description = "\n".join(
        description_lines
    )

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

        "SEQUENCE:0",

        "STATUS:CONFIRMED",

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

    today = datetime.date.today()

    end_date = (
        today
        + datetime.timedelta(
            days=args.days_ahead
        )
    )

    print(
        f"正在向 Finnhub 查詢 "
        f"{today} 到 {end_date} "
        f"的財報資料..."
    )

    # ========================================================
    # API
    # ========================================================

    raw_items = fetch_earnings(
        today,
        end_date,
        api_key
    )

    print(
        f"Finnhub 總共回傳 "
        f"{len(raw_items)} 筆財報資料"
        f"（含所有美股）"
    )

    # ========================================================
    # 篩選 watchlist
    # ========================================================

    events = []

    for item in raw_items:

        symbol = (
            item.get("symbol", "")
            .upper()
        )

        if symbol in watchlist:

            events.append(item)

    # ========================================================
    # 排序
    # ========================================================

    events.sort(
        key=event_sort_key
    )

    print(
        f"篩選後，屬於觀察名單的 "
        f"財報事件共 {len(events)} 筆"
    )

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
        "//Watchlist 2.0"
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

    for item in events:

        body += (
            "\r\n"
            + build_event(
                item,
                now_stamp,
                args.alarm_hours_before
            )
        )

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
        f"{len(events)} 個事件"
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
