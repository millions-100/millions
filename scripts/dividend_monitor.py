import json
import os
import re
import sys
from datetime import date, datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

STATE_PATH = Path("data/dividend-monitor-state.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; ETFDividendMonitor/3.0)"}
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


def send_telegram(message: str):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    r.raise_for_status()


def get_soup(url: str) -> BeautifulSoup:
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def parse_date_value(value: str):
    m = re.search(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", value)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def fmt_date(d):
    if not d:
        return "-"
    wd = ["월", "화", "수", "목", "금", "토", "일"][d.weekday()]
    return f"{d:%Y-%m-%d}({wd})"


def extract_labeled_date(text: str, label: str):
    # Example:
    # 다음 기준일 2026.10.15
    # 배당락일 2026.10.14
    # 마지막 매수일 2026.10.13
    # 지급 예정일 2026.10.19
    pattern = re.compile(
        re.escape(label) + r"\s*(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})"
    )
    m = pattern.search(text)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_distribution_table(soup: BeautifulSoup):
    """
    Find the exact dividend-history table:
      지급일 | 기준일 | 주당 | 분배율
    and parse ONLY the first data row.
    """
    target = None

    for table in soup.find_all("table"):
        headers = [
            re.sub(r"\s+", "", th.get_text(" ", strip=True))
            for th in table.find_all("th")
        ]
        joined = "|".join(headers)
        if all(key in joined for key in ("지급일", "기준일", "주당")):
            target = table
            break

    if target is None:
        # Some pages use non-table responsive markup.
        # Find a row-like text pattern with exact four fields.
        text = soup.get_text(" ", strip=True)
        pattern = re.compile(
            r"(20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2})\s+"
            r"(20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2})\s+"
            r"(\d{1,5})\s*원\s+"
            r"(\d+(?:\.\d+)?)\s*%"
        )
        m = pattern.search(text)
        if not m:
            raise RuntimeError("분배금 표(지급일/기준일/주당/분배율)를 찾지 못했습니다.")
        return {
            "payment_date": parse_date_value(m.group(1)),
            "record_date": parse_date_value(m.group(2)),
            "amount": int(m.group(3)),
            "yield_pct": float(m.group(4)),
        }

    # Prefer tbody rows; fall back to all tr after header.
    rows = target.find_all("tr")
    for tr in rows:
        cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
        if len(cells) < 3:
            continue

        payment = parse_date_value(cells[0])
        record = parse_date_value(cells[1])
        amount_match = re.search(r"(\d{1,5})\s*원", cells[2])

        if payment and record and amount_match:
            yield_pct = None
            if len(cells) >= 4:
                ym = re.search(r"(\d+(?:\.\d+)?)\s*%", cells[3])
                if ym:
                    yield_pct = float(ym.group(1))

            return {
                "payment_date": payment,
                "record_date": record,
                "amount": int(amount_match.group(1)),
                "yield_pct": yield_pct,
            }

    raise RuntimeError("분배금 표에서 유효한 첫 데이터 행을 찾지 못했습니다.")


def parse_fund(code: str):
    info = FUNDS[code]
    soup = get_soup(info["url"])
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    latest = parse_distribution_table(soup)

    next_record = extract_labeled_date(text, "다음 기준일")
    ex_date = extract_labeled_date(text, "배당락일")
    last_buy = extract_labeled_date(text, "마지막 매수일")
    next_payment = extract_labeled_date(text, "지급 예정일")

    # Schedule is trusted only when all 4 explicit labels exist.
    schedule_verified = all([next_record, ex_date, last_buy, next_payment])

    return {
        "code": code,
        "name": info["name"],
        "amount": latest["amount"],
        "latest_payment_date": latest["payment_date"],
        "latest_record_date": latest["record_date"],
        "latest_yield_pct": latest["yield_pct"],
        "last_buy_date": last_buy if schedule_verified else None,
        "ex_date": ex_date if schedule_verified else None,
        "record_date": next_record if schedule_verified else None,
        "payment_date": next_payment if schedule_verified else None,
        "schedule_verified": schedule_verified,
        "source_url": info["url"],
    }


def load_state():
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def event_key(x):
    # Key is based on the latest ACTUAL distribution row, not random page numbers.
    return (
        f'{x["code"]}:'
        f'{x["latest_record_date"].isoformat()}:'
        f'{x["latest_payment_date"].isoformat()}:'
        f'{x["amount"]}'
    )


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

    yield_line = ""
    if x["latest_yield_pct"] is not None:
        yield_line = f"📊 최근 분배율: {x['latest_yield_pct']:.2f}%\n"

    if x["schedule_verified"]:
        schedule = (
            f"\n📌 <b>다음 분배 일정</b>\n"
            f"🛒 마지막 매수일: <b>{fmt_date(x['last_buy_date'])}</b>\n"
            "⚠️ 위 날짜 장 마감 전 <b>실제 체결 완료</b> 필요\n"
            f"📉 분배락일: {fmt_date(x['ex_date'])}\n"
            f"📅 지급기준일: {fmt_date(x['record_date'])}\n"
            f"💳 지급 예정일: {fmt_date(x['payment_date'])}\n"
        )
    else:
        schedule = (
            "\n📌 <b>다음 분배 일정</b>\n"
            "⚠️ 상세페이지에서 다음 일정을 아직 확인하지 못했습니다.\n"
        )

    msg = (
        "💰 <b>ETF 분배금 업데이트</b>\n\n"
        f"<b>{escape(x['name'])} ({x['code']})</b>\n"
        f"💵 최근 실제 1주당 분배금: <b>{x['amount']:,}원</b>{change}\n"
        f"📆 최근 기준일: {fmt_date(x['latest_record_date'])}\n"
        f"💳 최근 지급일: {fmt_date(x['latest_payment_date'])}\n"
        f"{yield_line}"
        f"{schedule}\n"
        "✅ 분배금 표의 <b>주당</b> 열에서 직접 읽음\n"
        "ℹ️ ETF Retro 표는 KRX 종가·세이브로 분배금 자료 기반\n"
        f'🔗 <a href="{x["source_url"]}">분배금 상세 확인</a>'
    )
    send_telegram(msg)


def remind_last_buy(x):
    if not x["schedule_verified"] or not x["last_buy_date"]:
        return

    msg = (
        "🚨 <b>오늘이 분배금 수취 마지막 매수일</b>\n\n"
        f"<b>{escape(x['name'])} ({x['code']})</b>\n"
        f"🛒 오늘 {fmt_date(x['last_buy_date'])} 장 마감 전 <b>실제 체결 완료</b> 필요\n"
        f"📉 분배락일: {fmt_date(x['ex_date'])}\n"
        f"📅 지급기준일: {fmt_date(x['record_date'])}\n"
        f"💳 지급 예정일: {fmt_date(x['payment_date'])}"
    )
    send_telegram(msg)


def main():
    state = load_state()
    today = datetime.now(KST).date()
    send_initial = os.getenv("SEND_INITIAL", "false").lower() == "true"
    errors = []

    for code in FUNDS:
        try:
            x = parse_fund(code)
        except Exception as e:
            errors.append(f"{code}: {type(e).__name__}: {e}")
            continue

        print(
            f"{code}: amount={x['amount']} "
            f"latest_record={x['latest_record_date']} "
            f"next_record={x['record_date']} "
            f"verified={x['schedule_verified']}"
        )

        s = state.setdefault(code, {})
        key = event_key(x)
        is_first = not s.get("event_key")

        if s.get("event_key") != key:
            if (not is_first) or send_initial:
                announce(x, s.get("amount"))

            s.update({
                "event_key": key,
                "amount": x["amount"],
                "latest_record_date": x["latest_record_date"].isoformat(),
                "latest_payment_date": x["latest_payment_date"].isoformat(),
                "last_buy_date": x["last_buy_date"].isoformat() if x["last_buy_date"] else None,
                "ex_date": x["ex_date"].isoformat() if x["ex_date"] else None,
                "record_date": x["record_date"].isoformat() if x["record_date"] else None,
                "payment_date": x["payment_date"].isoformat() if x["payment_date"] else None,
                "schedule_verified": x["schedule_verified"],
                "reminder_sent_for": None,
            })

        if (
            x["schedule_verified"]
            and x["last_buy_date"] == today
            and s.get("reminder_sent_for") != today.isoformat()
        ):
            remind_last_buy(x)
            s["reminder_sent_for"] = today.isoformat()

    save_state(state)

    if errors:
        print("\n".join(errors), file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
