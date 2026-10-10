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
UA = {
    "User-Agent": "Mozilla/5.0 (compatible; ETFDividendMonitor/2.0; +https://github.com/)"
}
KST = ZoneInfo("Asia/Seoul")
KR_HOLIDAYS = holidays.KR()

FUNDS = {
    "441640": {
        "name": "KODEX 미국배당커버드콜액티브",
        "manager": "KODEX",
        "official_search": "https://www.samsungfund.com/etf/search.do?searchText=441640",
        "fallback": "https://www.etfretro.kr/etf/441640/",
    },
    "441680": {
        "name": "TIGER 미국나스닥100커버드콜(합성)",
        "manager": "TIGER",
        "official_list": "https://www.tigeretf.com/ko/customer/notice/list.do",
        "fallback": "https://www.etfretro.kr/etf/441680/",
    },
}


def is_business_day(d: date):
    # Used only as a fallback when the official notice omits a date.
    return d.weekday() < 5 and d not in KR_HOLIDAYS


def previous_business_days(d: date, n: int):
    result = []
    cur = d - timedelta(days=1)
    while len(result) < n:
        if is_business_day(cur):
            result.append(cur)
        cur -= timedelta(days=1)
    return list(reversed(result))


def next_business_days(d: date, n: int):
    result = []
    cur = d + timedelta(days=1)
    while len(result) < n:
        if is_business_day(cur):
            result.append(cur)
        cur += timedelta(days=1)
    return result


def fmt_date(d):
    if not d:
        return "-"
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


def infer_year(month, day, reference):
    year = reference.year
    candidate = date(year, month, day)
    # Handles Dec -> Jan payment transitions.
    if candidate < reference - timedelta(days=180):
        candidate = date(year + 1, month, day)
    elif candidate > reference + timedelta(days=180):
        candidate = date(year - 1, month, day)
    return candidate


def parse_any_date(text, reference=None):
    # Full date first.
    m = re.search(r"(20\d{2})[.\-/년\s]+(\d{1,2})[.\-/월\s]+(\d{1,2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    # Month/day format, using notice publication/reference year.
    m = re.search(r"(?<!\d)(\d{1,2})\s*[./]\s*(\d{1,2})(?!\d)", text)
    if m and reference:
        try:
            return infer_year(int(m.group(1)), int(m.group(2)), reference)
        except ValueError:
            pass
    return None


def parse_notice_publish_date(soup):
    text = soup.get_text(" ", strip=True)
    # Prefer yyyy.mm.dd / yyyy-mm-dd forms.
    dates = []
    for m in re.finditer(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", text):
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if date(2023, 1, 1) <= d <= date.today() + timedelta(days=30):
                dates.append(d)
        except ValueError:
            pass
    return dates[0] if dates else date.today()


def parse_amount_from_notice(soup, code):
    for tr in soup.find_all("tr"):
        row = " ".join(tr.stripped_strings)
        if code not in row:
            continue

        # Remove code and percentages before considering integer amounts.
        cleaned = row.replace(code, " ")
        cleaned = re.sub(r"\d+(?:\.\d+)?\s*%", " ", cleaned)
        nums = [int(x.replace(",", "")) for x in re.findall(r"(?<![\d.])\d{1,5}(?:,\d{3})?(?![\d.])", cleaned)]
        # Distribution amounts for these funds are normally tens/hundreds of won.
        candidates = [x for x in nums if 10 <= x <= 5000]
        if candidates:
            # Usually the amount is the last plain integer in the row.
            return candidates[-1]

    # Text fallback near the code.
    text = soup.get_text(" ", strip=True)
    i = text.find(code)
    if i >= 0:
        chunk = text[i:i+350]
        m = re.search(r"(?:분배금[^0-9]{0,30})?(\d{2,4})\s*원", chunk)
        if m:
            return int(m.group(1))
    raise RuntimeError(f"{code}: official notice amount parse failed")


def parse_schedule_rows(soup, reference):
    result = {
        "announcement_date": None,
        "last_buy_date": None,
        "ex_date": None,
        "record_date": None,
        "payment_date": None,
    }

    rows = []
    for tr in soup.find_all("tr"):
        row = " ".join(tr.stripped_strings)
        if row:
            rows.append(row)

    # Detailed schedule tables on both KODEX and TIGER pages use these labels.
    label_map = [
        ("분배금 공시일", "announcement_date"),
        ("분배락 전일", "announcement_date"),
        ("분배락일", "ex_date"),
        ("지급기준일", "record_date"),
        ("분배금 지급일", "payment_date"),
        ("지급일 (예정)", "payment_date"),
        ("지급일(예정)", "payment_date"),
    ]

    for row in rows:
        for label, key in label_map:
            if label in row and result[key] is None:
                d = parse_any_date(row, reference)
                if d:
                    result[key] = d

        # Some notices explicitly say "X월 X일까지 ETF 매수 시..."
        if ("ETF" in row and "매수" in row and ("수취" in row or "분배금" in row)):
            d = parse_any_date(row, reference)
            if d:
                result["last_buy_date"] = d

    # Search the whole notice for explicit "까지 ETF 매수" language.
    text = soup.get_text(" ", strip=True)
    m = re.search(
        r"(?:(20\d{2})[.\-/년\s]*)?(\d{1,2})[./월\s]+(\d{1,2})일?.{0,35}?까지.{0,20}?ETF\s*매수",
        text,
    )
    if m:
        y = int(m.group(1)) if m.group(1) else reference.year
        try:
            result["last_buy_date"] = date(y, int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    if result["last_buy_date"] is None and result["announcement_date"]:
        # In both managers' distribution notices, the announcement day is
        # commonly also the final day to buy before ex-date.
        result["last_buy_date"] = result["announcement_date"]

    # Fill only missing values.
    if result["record_date"] and result["ex_date"] is None:
        p = previous_business_days(result["record_date"], 1)
        result["ex_date"] = p[-1]
    if result["record_date"] and result["last_buy_date"] is None:
        p = previous_business_days(result["record_date"], 2)
        result["last_buy_date"] = p[0]
    if result["record_date"] and result["payment_date"] is None:
        result["payment_date"] = next_business_days(result["record_date"], 2)[-1]

    return result


def normalize_result(code, amount, schedule, source_url, source_name):
    if not schedule.get("record_date"):
        raise RuntimeError(f"{code}: official notice record date missing")

    return {
        "code": code,
        "name": FUNDS[code]["name"],
        "amount": amount,
        "record_date": schedule["record_date"],
        "payment_date": schedule.get("payment_date"),
        "last_buy_date": schedule.get("last_buy_date"),
        "ex_date": schedule.get("ex_date"),
        "source_url": source_url,
        "source_name": source_name,
        "official": True,
    }


def discover_kodex_notice():
    info = FUNDS["441640"]
    soup = get_soup(info["official_search"])
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        title = " ".join(a.stripped_strings)
        if "notice-view.do" in href and ("배당" in title or "분배금" in title or "월중" in title):
            url = urljoin("https://www.samsungfund.com", href)
            if url not in links:
                links.append(url)

    # Search pages sometimes place the title text outside <a>, so include all notice links too.
    if not links:
        for a in soup.find_all("a", href=True):
            if "notice-view.do" in a["href"]:
                url = urljoin("https://www.samsungfund.com", a["href"])
                if url not in links:
                    links.append(url)

    for url in links[:20]:
        try:
            s = get_soup(url)
            text = s.get_text(" ", strip=True)
            if "441640" in text and "분배금" in text:
                return url, s
        except Exception:
            continue
    raise RuntimeError("441640: latest official KODEX notice not found")


def discover_tiger_notice():
    info = FUNDS["441680"]
    soup = get_soup(info["official_list"])
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "notice/view.do" in href and "detailsKey=" in href:
            url = urljoin("https://www.tigeretf.com", href)
            if url not in links:
                links.append(url)

    # Visit recent notice pages and choose the newest containing both the code and distribution wording.
    candidates = []
    for url in links[:40]:
        try:
            s = get_soup(url)
            text = s.get_text(" ", strip=True)
            if "441680" in text and "분배금" in text:
                pub = parse_notice_publish_date(s)
                candidates.append((pub, url, s))
        except Exception:
            continue

    if not candidates:
        raise RuntimeError("441680: latest official TIGER notice not found")

    candidates.sort(key=lambda x: x[0], reverse=True)
    _, url, s = candidates[0]
    return url, s


def parse_official(code):
    if code == "441640":
        url, soup = discover_kodex_notice()
        manager = "KODEX 공식 공지"
    elif code == "441680":
        url, soup = discover_tiger_notice()
        manager = "TIGER 공식 공지"
    else:
        raise RuntimeError(f"Unsupported code: {code}")

    pub = parse_notice_publish_date(soup)
    amount = parse_amount_from_notice(soup, code)
    schedule = parse_schedule_rows(soup, pub)

    return normalize_result(
        code=code,
        amount=amount,
        schedule=schedule,
        source_url=url,
        source_name=manager,
    )


def parse_fallback(code):
    """ETF Retro backup parser. Used only when official parsing fails."""
    url = FUNDS[code]["fallback"]
    soup = get_soup(url)
    text = soup.get_text(" ", strip=True)

    # Try common history-row formats.
    pattern = re.compile(
        r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})"
        r".{0,80}?(\d{1,5})\s*원"
    )
    matches = []
    for m in pattern.finditer(text):
        y, mo, d, amount = map(int, m.groups())
        try:
            rd = date(y, mo, d)
        except ValueError:
            continue
        if 10 <= amount <= 5000:
            matches.append((rd, amount))

    if not matches:
        raise RuntimeError(f"{code}: backup source parse failed")

    matches.sort(key=lambda x: x[0], reverse=True)
    record_date, amount = matches[0]
    prev2 = previous_business_days(record_date, 2)

    return {
        "code": code,
        "name": FUNDS[code]["name"],
        "amount": amount,
        "record_date": record_date,
        "payment_date": None,
        "last_buy_date": prev2[0],
        "ex_date": prev2[1],
        "source_url": url,
        "source_name": "ETF Retro 백업",
        "official": False,
    }


def get_latest(code):
    official_error = None
    try:
        official = parse_official(code)
    except Exception as e:
        official_error = e
        official = None

    try:
        fallback = parse_fallback(code)
    except Exception:
        fallback = None

    if official and fallback:
        # Official is preferred unless the fallback clearly contains a newer record,
        # which can happen if an official site's HTML changes and an older notice is parsed.
        if official["record_date"] >= fallback["record_date"]:
            return official
        fallback["source_name"] += " (공식 파서보다 최신 회차 감지)"
        return fallback

    if official:
        return official
    if fallback:
        fallback["source_name"] += f" / 공식 파싱 실패: {type(official_error).__name__}"
        return fallback

    raise RuntimeError(f"{code}: both official and backup sources failed: {official_error}")


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


def source_badge(x):
    return "✅ 공식 운용사 공지" if x.get("official") else "⚠️ 백업 데이터"


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
        f"💳 지급일: {fmt_date(x['payment_date'])}\n\n"
        f"{source_badge(x)} · {escape(x['source_name'])}\n"
        f'🔗 <a href="{x["source_url"]}">출처 확인</a>'
    )
    send_telegram(msg)


def remind_last_buy(x):
    msg = (
        "🚨 <b>오늘이 분배금 수취 마지막 매수일</b>\n\n"
        f"<b>{escape(x['name'])} ({x['code']})</b>\n"
        f"💵 최근 확인 분배금: <b>{x['amount']:,}원/주</b>\n"
        f"🛒 오늘 {fmt_date(x['last_buy_date'])} 장 마감 전 <b>실제 체결 완료</b> 필요\n"
        f"📉 분배락일: {fmt_date(x['ex_date'])}\n"
        f"📅 지급기준일: {fmt_date(x['record_date'])}\n\n"
        f"{source_badge(x)} · {escape(x['source_name'])}"
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
            f"{code}: {x['amount']}원 / record={x['record_date']} / "
            f"source={x['source_name']} / official={x['official']}"
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
                "record_date": x["record_date"].isoformat(),
                "payment_date": x["payment_date"].isoformat() if x["payment_date"] else None,
                "last_buy_date": x["last_buy_date"].isoformat() if x["last_buy_date"] else None,
                "ex_date": x["ex_date"].isoformat() if x["ex_date"] else None,
                "source_name": x["source_name"],
                "official": x["official"],
                "reminder_sent_for": None,
            })

        if (
            x.get("last_buy_date")
            and today == x["last_buy_date"]
            and s.get("reminder_sent_for") != x["last_buy_date"].isoformat()
        ):
            remind_last_buy(x)
            s["reminder_sent_for"] = x["last_buy_date"].isoformat()

    save_state(state)

    if errors:
        print("\n".join(errors), file=sys.stderr)
        # One site outage should not disable the other fund.
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
