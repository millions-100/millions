import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import holidays

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

STATE_PATH = Path("data/dividend-monitor-state.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; ETFDividendMonitor/2.1)"}
KST = ZoneInfo("Asia/Seoul")
KR_HOLIDAYS = holidays.KR()

FUNDS = {
    "441640": {
        "name": "KODEX 미국배당커버드콜액티브",
        "manager": "KODEX",
        "official": "https://www.samsungfund.com/etf/lounge/notice.do?category=DIVIDEND",
        "fallback": "https://www.etfretro.kr/etf/441640/",
    },
    "441680": {
        "name": "TIGER 미국나스닥100커버드콜(합성)",
        "manager": "TIGER",
        "official": "https://www.tigeretf.com/ko/customer/notice/list.do",
        "fallback": "https://www.etfretro.kr/etf/441680/",
    },
}


def fmt_date(d):
    if not d:
        return "공식 확인 중"
    wd = ["월", "화", "수", "목", "금", "토", "일"][d.weekday()]
    return f"{d:%Y-%m-%d}({wd})"


def send_telegram(message):
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


def get_soup(url):
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def extract_full_dates(text):
    out = []
    for m in re.finditer(r"(20\d{2})[.\-/년\s]+(\d{1,2})[.\-/월\s]+(\d{1,2})", text):
        try:
            out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    return out


def parse_amount_near_code(text, code):
    i = text.find(code)
    if i < 0:
        return None
    chunk = text[i:i+500]
    # Prefer values explicitly followed by 원.
    nums = [int(x.replace(",", "")) for x in re.findall(r"(\d{2,5}(?:,\d{3})?)\s*원", chunk)]
    nums = [x for x in nums if 10 <= x <= 5000]
    if nums:
        return nums[0]
    return None


def parse_schedule_from_notice(soup):
    text = soup.get_text(" ", strip=True)
    result = {
        "last_buy_date": None,
        "ex_date": None,
        "record_date": None,
        "payment_date": None,
    }

    # Read table/text rows by semantic labels.
    rows = []
    for tr in soup.find_all("tr"):
        row = " ".join(tr.stripped_strings)
        if row:
            rows.append(row)
    rows.append(text)

    label_patterns = {
        "ex_date": ["분배락일"],
        "record_date": ["지급기준일", "분배금 지급기준일"],
        "payment_date": ["지급일", "분배금 지급일", "지급 예정일"],
    }

    for key, labels in label_patterns.items():
        for row in rows:
            if any(label in row for label in labels):
                dates = extract_full_dates(row)
                if dates:
                    result[key] = dates[0]
                    break

    # Explicit final-buy wording only. Do NOT infer it from fallback source.
    patterns = [
        r"(20\d{2})[.\-/년\s]+(\d{1,2})[.\-/월\s]+(\d{1,2})일?.{0,40}?까지.{0,30}?매수",
        r"매수.{0,30}?(20\d{2})[.\-/년\s]+(\d{1,2})[.\-/월\s]+(\d{1,2})일?.{0,30}?까지",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            try:
                result["last_buy_date"] = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                break
            except ValueError:
                pass

    # If official notice has ex-date but not final-buy date, one business day before ex-date
    # is a standard settlement-rule inference. We mark it inferred.
    inferred_last_buy = False
    if result["last_buy_date"] is None and result["ex_date"]:
        cur = result["ex_date"] - timedelta(days=1)
        while cur.weekday() >= 5 or cur in KR_HOLIDAYS:
            cur -= timedelta(days=1)
        result["last_buy_date"] = cur
        inferred_last_buy = True

    result["inferred_last_buy"] = inferred_last_buy
    return result


def find_latest_kodex_notice():
    root = get_soup(FUNDS["441640"]["official"])
    links = []
    for a in root.find_all("a", href=True):
        href = a["href"]
        title = " ".join(a.stripped_strings)
        if "notice-view.do" in href and ("분배" in title or "배당" in title):
            links.append(urljoin("https://www.samsungfund.com", href))
    for url in links[:30]:
        try:
            s = get_soup(url)
            t = s.get_text(" ", strip=True)
            if "441640" in t and "분배" in t:
                return url, s
        except Exception:
            continue
    raise RuntimeError("KODEX official notice not found")


def find_latest_tiger_notice():
    root = get_soup(FUNDS["441680"]["official"])
    links = []
    for a in root.find_all("a", href=True):
        href = a["href"]
        if "notice/view.do" in href and "detailsKey=" in href:
            links.append(urljoin("https://www.tigeretf.com", href))
    for url in links[:40]:
        try:
            s = get_soup(url)
            t = s.get_text(" ", strip=True)
            if "441680" in t and "분배금" in t:
                return url, s
        except Exception:
            continue
    raise RuntimeError("TIGER official notice not found")


def parse_official(code):
    if code == "441640":
        url, soup = find_latest_kodex_notice()
        src = "KODEX 공식 공지"
    else:
        url, soup = find_latest_tiger_notice()
        src = "TIGER 공식 공지"

    text = soup.get_text(" ", strip=True)
    amount = parse_amount_near_code(text, code)
    if amount is None:
        raise RuntimeError(f"{code}: official amount parse failed")

    schedule = parse_schedule_from_notice(soup)

    # We require at least ex-date + record-date from the official notice
    # before treating the schedule as verified.
    schedule_verified = bool(schedule["ex_date"] and schedule["record_date"])

    return {
        "code": code,
        "name": FUNDS[code]["name"],
        "amount": amount,
        "last_buy_date": schedule["last_buy_date"] if schedule_verified else None,
        "ex_date": schedule["ex_date"] if schedule_verified else None,
        "record_date": schedule["record_date"] if schedule_verified else None,
        "payment_date": schedule["payment_date"] if schedule_verified else None,
        "source_url": url,
        "source_name": src,
        "official": True,
        "schedule_verified": schedule_verified,
        "inferred_last_buy": schedule.get("inferred_last_buy", False),
    }


def parse_fallback_amount_only(code):
    """Backup source is used for amount only. Dates are intentionally ignored."""
    url = FUNDS[code]["fallback"]
    soup = get_soup(url)
    text = soup.get_text(" ", strip=True)

    # Search around fund code first.
    amount = parse_amount_near_code(text, code)

    # Generic fallback for recent distribution history rows.
    if amount is None:
        vals = [int(x.replace(",", "")) for x in re.findall(r"(\d{2,5}(?:,\d{3})?)\s*원", text)]
        vals = [x for x in vals if 10 <= x <= 5000]
        if vals:
            amount = vals[0]

    if amount is None:
        raise RuntimeError(f"{code}: fallback amount parse failed")

    return {
        "code": code,
        "name": FUNDS[code]["name"],
        "amount": amount,
        "last_buy_date": None,
        "ex_date": None,
        "record_date": None,
        "payment_date": None,
        "source_url": url,
        "source_name": "ETF Retro 백업",
        "official": False,
        "schedule_verified": False,
        "inferred_last_buy": False,
    }


def get_latest(code):
    official_error = None
    try:
        return parse_official(code)
    except Exception as e:
        official_error = e

    backup = parse_fallback_amount_only(code)
    backup["source_name"] += f" / 공식 파싱 실패: {type(official_error).__name__}"
    return backup


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
    # When official schedule is unavailable, amount change alone can trigger update.
    rd = x["record_date"].isoformat() if x["record_date"] else "unknown"
    return f'{x["code"]}:{rd}:{x["amount"]}:{x["schedule_verified"]}'


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

    if x["schedule_verified"]:
        schedule_block = (
            f"🛒 마지막 매수일: <b>{fmt_date(x['last_buy_date'])}</b>\n"
            "⚠️ 위 날짜 장 마감 전 <b>실제 체결 완료</b> 필요\n"
            f"📉 분배락일: {fmt_date(x['ex_date'])}\n"
            f"📅 지급기준일: {fmt_date(x['record_date'])}\n"
            f"💳 지급일: {fmt_date(x['payment_date'])}\n"
        )
        if x.get("inferred_last_buy"):
            schedule_block += "ℹ️ 마지막 매수일은 공식 분배락일 기준으로 계산\n"
    else:
        schedule_block = (
            "🛒 마지막 매수일: <b>공식 확인 중</b>\n"
            "📉 분배락일: 공식 확인 중\n"
            "📅 지급기준일: 공식 확인 중\n"
            "💳 지급일: 공식 확인 중\n"
            "⚠️ 공식 일정이 확인될 때까지 날짜를 임의 계산하지 않음\n"
        )

    badge = "✅ 공식 운용사 공지" if x["official"] else "⚠️ 백업 데이터"

    msg = (
        "💰 <b>ETF 분배금 업데이트</b>\n\n"
        f"<b>{escape(x['name'])} ({x['code']})</b>\n"
        f"💵 1주당 분배금: <b>{x['amount']:,}원</b>{change}\n"
        + schedule_block +
        f"\n{badge} · {escape(x['source_name'])}\n"
        f'🔗 <a href="{x["source_url"]}">출처 확인</a>'
    )
    send_telegram(msg)


def remind_last_buy(x):
    # Reminder is only sent when the schedule is verified.
    if not x["schedule_verified"] or not x["last_buy_date"]:
        return

    msg = (
        "🚨 <b>오늘이 분배금 수취 마지막 매수일</b>\n\n"
        f"<b>{escape(x['name'])} ({x['code']})</b>\n"
        f"💵 확인 분배금: <b>{x['amount']:,}원/주</b>\n"
        f"🛒 오늘 {fmt_date(x['last_buy_date'])} 장 마감 전 <b>실제 체결 완료</b> 필요\n"
        f"📉 분배락일: {fmt_date(x['ex_date'])}\n"
        f"📅 지급기준일: {fmt_date(x['record_date'])}\n"
        "✅ 공식 일정 확인 완료"
    )
    send_telegram(msg)


def main():
    state = load_state()
    today = datetime.now(KST).date()
    errors = []

    for code in FUNDS:
        try:
            x = get_latest(code)
        except Exception as e:
            errors.append(f"{code}: {e}")
            continue

        print(
            f"{code}: amount={x['amount']} verified={x['schedule_verified']} "
            f"source={x['source_name']}"
        )

        s = state.setdefault(code, {})
        key = event_key(x)

        is_first = not s.get("event_key")
        send_initial = os.getenv("SEND_INITIAL", "false").lower() == "true"

        if s.get("event_key") != key:
            if (not is_first) or send_initial:
                announce(x, s.get("amount"))

            s.update({
                "event_key": key,
                "amount": x["amount"],
                "record_date": x["record_date"].isoformat() if x["record_date"] else None,
                "payment_date": x["payment_date"].isoformat() if x["payment_date"] else None,
                "last_buy_date": x["last_buy_date"].isoformat() if x["last_buy_date"] else None,
                "ex_date": x["ex_date"].isoformat() if x["ex_date"] else None,
                "source_name": x["source_name"],
                "official": x["official"],
                "schedule_verified": x["schedule_verified"],
                "reminder_sent_for": None,
            })

        if (
            x["schedule_verified"]
            and x["last_buy_date"]
            and today == x["last_buy_date"]
            and s.get("reminder_sent_for") != x["last_buy_date"].isoformat()
        ):
            remind_last_buy(x)
            s["reminder_sent_for"] = x["last_buy_date"].isoformat()

    save_state(state)

    if errors:
        print("\n".join(errors), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
