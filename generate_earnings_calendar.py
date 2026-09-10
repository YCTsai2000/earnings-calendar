#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自訂觀察名單財報行事曆產生器（Finnhub 版）
"""

import argparse
import datetime
import os
import sys

try:
    import requests
except ImportError:
    sys.exit("找不到 requests，請先執行：pip install requests --break-system-packages")


FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"

HOUR_LABEL = {
    "bmo": "盤前公布（美股開盤前）",
    "amc": "盤後公布（美股收盤後）",
    "dmh": "盤中公布",
}


def load_watchlist(path: str) -> set:
    """
    讀取觀察名單檔案。格式規則：
      - 一行一個股票代號
      - 以 # 開頭的整行視為註解，會被忽略
      - 空白行會被忽略
      - 大小寫不敏感（內部一律轉大寫比對）
    """
    if not os.path.exists(path):
        sys.exit(f"[錯誤] 找不到觀察名單檔案：{path}")

    symbols = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            symbol = line.split("#")[0].strip()
            if symbol:
                symbols.add(symbol.upper())
    return symbols


def fetch_earnings(start_date: datetime.date, end_date: datetime.date, api_key: str):
    """一次呼叫 Finnhub，取得整段日期範圍的財報資料。"""
    params = {
        "from": start_date.strftime("%Y-%m-%d"),
        "to": end_date.strftime("%Y-%m-%d"),
        "token": api_key,
    }
    resp = requests.get(FINNHUB_URL, params=params, timeout=30)

    if resp.status_code == 401:
        sys.exit("[錯誤] Finnhub 回傳401：API金鑰無效，請確認 FINNHUB_API_KEY 是否正確")
    if resp.status_code == 403:
        sys.exit(
            "[錯誤] Finnhub 回傳403：這個端點可能已經被限制在付費方案，"
            "請登入 finnhub.io/dashboard 確認你的方案權限"
        )
    resp.raise_for_status()

    data = resp.json()
    return data.get("earningsCalendar", [])


def ics_escape(text: str) -> str:
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold_line(line: str, limit: int = 75) -> str:
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
        folded += "\r\n " + chunk.decode("utf-8")
    return folded


def fmt_num(x):
    if x is None or x == "":
        return "無資料"
    return str(x)


def build_event(item: dict, now_stamp: str, alarm_hours_before: int) -> str:
    d = datetime.datetime.strptime(item["date"], "%Y-%m-%d").date()
    dtstart = d.strftime("%Y%m%d")
    dtend = (d + datetime.timedelta(days=1)).strftime("%Y%m%d")

    symbol = item.get("symbol", "").upper()
    hour_label = HOUR_LABEL.get(item.get("hour", ""), "時間未公布")

    summary = f"{symbol} 財報 (Q{item.get('quarter', '?')} {item.get('year', '')})"

    description = "\n".join(
        [
            f"股票代號：{symbol}",
            f"公布時間：{hour_label}",
            f"財測 EPS：{fmt_num(item.get('epsEstimate'))}",
            f"實際 EPS：{fmt_num(item.get('epsActual'))}",
            f"財測營收：{fmt_num(item.get('revenueEstimate'))}",
            f"實際營收：{fmt_num(item.get('revenueActual'))}",
        ]
    )

    uid = f"earnings-{symbol}-{dtstart}@earnings-calendar-script"

    lines = [
        "BEGIN:VEVENT",
        f"DTSTART;VALUE=DATE:{dtstart}",
        f"DTEND;VALUE=DATE:{dtend}",
        f"DTSTAMP:{now_stamp}",
        f"UID:{uid}",
        f"CREATED:{now_stamp}",
        f"DESCRIPTION:{ics_escape(description)}",
        f"LAST-MODIFIED:{now_stamp}",
        "SEQUENCE:0",
        "STATUS:CONFIRMED",
        f"SUMMARY:{ics_escape(summary)}",
        "TRANSP:OPAQUE",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{ics_escape(summary)}",
        f"TRIGGER:-PT{alarm_hours_before}H",
        "END:VALARM",
        "END:VEVENT",
    ]
    return "\r\n".join(fold_line(l) for l in lines)


def main():
    parser = argparse.ArgumentParser(description="產生自訂觀察名單的財報 .ics 行事曆檔（Finnhub 版）")
    parser.add_argument("--watchlist", type=str, default="watchlist.txt", help="觀察名單檔案路徑")
    parser.add_argument("--days-ahead", type=int, default=45, help="從今天起往後抓幾天的財報")
    parser.add_argument("--output", type=str, default="docs/earnings.ics", help="輸出檔案路徑")
    parser.add_argument(
        "--alarm-hours-before", type=int, default=28,
        help="提醒設在事件前幾小時（預設28小時＝前一天晚上8點）",
    )
    args = parser.parse_args()

    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        sys.exit("[錯誤] 找不到環境變數 FINNHUB_API_KEY，請先設定好金鑰再執行")

    watchlist = load_watchlist(args.watchlist)
    print(f"觀察名單共 {len(watchlist)} 檔：{', '.join(sorted(watchlist))}")

    today = datetime.date.today()
    end_date = today + datetime.timedelta(days=args.days_ahead)
    print(f"正在向 Finnhub 查詢 {today} 到 {end_date} 的財報資料...")
    raw_items = fetch_earnings(today, end_date, api_key)
    print(f"Finnhub 總共回傳 {len(raw_items)} 筆財報資料（含所有美股）")

    events = [item for item in raw_items if item.get("symbol", "").upper() in watchlist]
    print(f"篩選後，屬於觀察名單的財報事件共 {len(events)} 筆")

    now_stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    body = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "PRODID:-//EarningsCalendarScript//Watchlist 1.0//EN",
            "VERSION:2.0",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            "X-WR-CALNAME:我的觀察名單財報行事曆（自動更新）",
            "X-WR-TIMEZONE:Asia/Taipei",
            "REFRESH-INTERVAL;VALUE=DURATION:P1D",
            "X-PUBLISHED-TTL:P1D",
        ]
    )
    for ev in events:
        body += "\r\n" + build_event(ev, now_stamp, args.alarm_hours_before)
    body += "\r\nEND:VCALENDAR\r\n"

    out_dir = os.path.dirname(args.output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(args.output, "w", encoding="utf-8", newline="") as f:
        f.write(body)

    print(f"已產生 {len(events)} 個事件 -> {args.output}")


if __name__ == "__main__":
    main()
