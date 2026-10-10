import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
import exchange_calendars as xcals

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

STATE_PATH = Path("data/dividend-monitor-state.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; ETFDividendMonitor/1.0)"}
KST = ZoneInfo("Asia/Seoul")

FUNDS = {
    "441640": {
        "name": "KODEX 미국배당커버드콜액티브",
        "url": "https://www.etfretro.kr/etf/441640/",
    },
    "441680": {
        "name": "TIGER 미국나스닥100커버드콜(합성)",
        "url": "https://www.etfretro.kr/etf/441680/",
    },
}

cal = xcals.get_calendar("XKRX")


def krx_sessions(start: date, end: date):
    idx = cal.sessions_in_range(start.isoformat(), end.isoformat())
    return [x.date() for x in idx]


def previous_sessions(d: date, n: int):
    sessions = krx_sessions(d - timedelta(days=20), d - timedelta(days=1))
    if len(sessions) < n:
        raise RuntimeError(f"Not enough KRX sessions before {d}")
    return sessions[-n:]


def fmt_date(d: date | None):
    if not d:
        return "-"
    weekday = ["월", "화", "수", "목", "금", "토", "일"][d.weekday()]
    return f"{d:%Y-%m-%d}({weekday})"


def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    r.raise_for_status()


def fetch_text(url: str):
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)


def parse_latest_distribution(code: str):
    info = FUNDS[code]
    text = fetch_text(info["url"])

    # ETF Retro pages expose recent records in a form equivalent to:
    # 2026.09.15 지급 09.17 96원
    pattern = re.compile(
        r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})"
        r".{0,40}?(?:지급|기준|분배)"
        r".{0,20}?(\d{1,2})[.\-/](\d{1,2})"
        r".{0,30}?(\d{1,5})\s*원"
    )

    matches = []
    for m in pattern.finditer(text):
        y, mo, d, pmo, pd, amount = map(int, m.groups())
        try:
            record_date = date(y, mo, d)
            payment_date = date(y, pmo, pd)
        except ValueError:
            continue

        # Around New Year's, payment can be in the next year.
        if payment_date < record_date - timedelta(days=200):
            payment_date = date(y + 1, pmo, pd)

        matches.append((record_date, payment_date, amount))

    # Fallback: some pages have amount before/after dates with extra wording.
    if not matches:
        loose = re.compile(
            r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2}).{0,120}?(\d{1,5})\s*원"
        )
        for m in loose.finditer(text):
            y, mo, d, amount = map(int, m.groups())
            try:
                record_date = date(y, mo, d)
            except ValueError:
                continue
            payment_date = None
            matches.append((record_date, payment_date, amount))

    if not matches:
        raise RuntimeError(f"{code}: could not parse a distribution record")

    matches.sort(key=lambda x: x[0], reverse=True)
    record_date, payment_date, amount = matches[0]

    prev2 = previous_sessions(record_date, 2)
    last_buy_date = prev2[0]
    ex_date = prev2[1]

    return {
        "code": code,
        "name": info["name"],
        "amount": amount,
        "record_date": record_date,
        "payment_date": payment_date,
        "last_buy_date": last_buy_date,
        "ex_date": ex_date,
        "source_url": info["url"],
    }


def load_state():
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def event_key(x):
    return f'{x["code"]}:{x["record_date"].isoformat()}:{x["amount"]}'


def announce(x, previous_amount=None):
    change = ""
    if previous_amount is not None:
        diff = x["amount"] - previous_amount
        if diff > 0:
            change = f" (+{diff:,}원)"
        elif diff < 0:
            change = f" ({diff:,}원)"
        else:
            change = " (변동 없음)"

    msg = (
        "💰 <b>ETF 분배금 업데이트</b>\n\n"
        f"<b>{escape(x['name'])} ({x['code']})</b>\n"
        f"💵 1주당 분배금: <b>{x['amount']:,}원</b>{change}\n"
        f"🛒 마지막 매수일: <b>{fmt_date(x['last_buy_date'])}</b>\n"
        "⚠️ 위 날짜 장 마감 전 <b>실제 체결 완료</b> 필요\n"
        f"📉 분배락일: {fmt_date(x['ex_date'])}\n"
        f"📅 지급기준일: {fmt_date(x['record_date'])}\n"
        f"💳 실제 지급일: {fmt_date(x['payment_date'])}\n\n"
        f'🔗 <a href="{x["source_url"]}">분배 이력 확인</a>'
    )
    send_telegram(msg)


def remind_last_buy(x):
    msg = (
        "🚨 <b>오늘이 분배금 수취 마지막 매수일</b>\n\n"
        f"<b>{escape(x['name'])} ({x['code']})</b>\n"
        f"💵 최근 확인 분배금: <b>{x['amount']:,}원/주</b>\n"
        f"🛒 오늘 {fmt_date(x['last_buy_date'])} 장 마감 전 <b>실제 체결 완료</b> 필요\n"
        f"📉 분배락일: {fmt_date(x['ex_date'])}\n"
        f"📅 지급기준일: {fmt_date(x['record_date'])}"
    )
    send_telegram(msg)


def main():
    state = load_state()
    today = datetime.now(KST).date()
    errors = []

    for code in FUNDS:
        try:
            x = parse_latest_distribution(code)
        except Exception as e:
            errors.append(f"{code}: {e}")
            continue

        s = state.setdefault(code, {})
        key = event_key(x)

        # First deployment: store current state without spamming old history.
        # Set SEND_INITIAL=true as a GitHub Actions variable/env if you want
        # the current known record sent immediately on the first run.
        is_first = not s.get("event_key")
        send_initial = os.getenv("SEND_INITIAL", "false").lower() == "true"

        if s.get("event_key") != key:
            if (not is_first) or send_initial:
                announce(x, s.get("amount"))
            s.update({
                "event_key": key,
                "amount": x["amount"],
                "record_date": x["record_date"].isoformat(),
                "payment_date": x["payment_date"].isoformat() if x["payment_date"] else None,
                "last_buy_date": x["last_buy_date"].isoformat(),
                "ex_date": x["ex_date"].isoformat(),
                "reminder_sent_for": None,
            })

        if (
            today == x["last_buy_date"]
            and s.get("reminder_sent_for") != x["last_buy_date"].isoformat()
        ):
            remind_last_buy(x)
            s["reminder_sent_for"] = x["last_buy_date"].isoformat()

    save_state(state)

    if errors:
        print("\n".join(errors), file=sys.stderr)
        # Don't fail everything if one site is temporarily unavailable.
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
