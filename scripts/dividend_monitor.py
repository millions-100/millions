import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

STATE_PATH = Path("data/dividend-monitor-state.json")
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0 Safari/537.36"
    )
}
KST = ZoneInfo("Asia/Seoul")

FUNDS = {
    "441640": {
        "name": "KODEX 미국배당커버드콜액티브",
        "retro": "https://www.etfretro.kr/etf/441640/",
        "official_distribution": "https://www.samsungfund.com/etf/product/distribution.do",
        "official_search": "https://www.samsungfund.com/etf/search.do?searchText=441640",
    },
    "441680": {
        "name": "TIGER 미국나스닥100커버드콜(합성)",
        "retro": "https://www.etfretro.kr/etf/441680/",
        "official_notice_list": "https://www.tigeretf.com/ko/customer/notice/list.do",
    },
}


def get_soup(url, timeout=30):
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def parse_date_value(value):
    m = re.search(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", value or "")
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


# ---------------------------------------------------------------------
# ETF Retro exact parser
# ---------------------------------------------------------------------

def extract_labeled_date(text, label):
    p = re.compile(
        re.escape(label) + r"\s*(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})"
    )
    m = p.search(text)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_retro_distribution_table(soup):
    """
    Reads only the structured dividend table:
    지급일 | 기준일 | 주당 | 분배율
    """
    target = None

    for table in soup.find_all("table"):
        headers = [
            re.sub(r"\s+", "", th.get_text(" ", strip=True))
            for th in table.find_all("th")
        ]
        joined = "|".join(headers)
        if all(x in joined for x in ("지급일", "기준일", "주당")):
            target = table
            break

    if target is not None:
        for tr in target.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
            if len(cells) < 3:
                continue

            payment = parse_date_value(cells[0])
            record = parse_date_value(cells[1])
            am = re.search(r"(\d{1,5})\s*원", cells[2])

            if payment and record and am:
                yp = None
                if len(cells) >= 4:
                    ym = re.search(r"(\d+(?:\.\d+)?)\s*%", cells[3])
                    if ym:
                        yp = float(ym.group(1))

                return {
                    "amount": int(am.group(1)),
                    "latest_record_date": record,
                    "latest_payment_date": payment,
                    "yield_pct": yp,
                }

    # Responsive fallback: exact four-column row only.
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    p = re.compile(
        r"(20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2})\s+"
        r"(20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2})\s+"
        r"(\d{1,5})\s*원\s+"
        r"(\d+(?:\.\d+)?)\s*%"
    )
    m = p.search(text)
    if not m:
        raise RuntimeError("ETF Retro 분배금 정형 표를 찾지 못했습니다.")

    return {
        "latest_payment_date": parse_date_value(m.group(1)),
        "latest_record_date": parse_date_value(m.group(2)),
        "amount": int(m.group(3)),
        "yield_pct": float(m.group(4)),
    }


def parse_retro(code):
    soup = get_soup(FUNDS[code]["retro"])
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    latest = parse_retro_distribution_table(soup)

    next_schedule = {
        "next_record_date": extract_labeled_date(text, "다음 기준일"),
        "ex_date": extract_labeled_date(text, "배당락일"),
        "last_buy_date": extract_labeled_date(text, "마지막 매수일"),
        "next_payment_date": extract_labeled_date(text, "지급 예정일"),
    }

    schedule_verified = all(next_schedule.values())

    return {
        **latest,
        **next_schedule,
        "schedule_verified": schedule_verified,
        "retro_url": FUNDS[code]["retro"],
    }


# ---------------------------------------------------------------------
# KODEX official cross-check
# ---------------------------------------------------------------------

def parse_kodex_official_distribution():
    soup = get_soup(FUNDS["441640"]["official_distribution"])

    for tr in soup.find_all("tr"):
        row_text = re.sub(r"\s+", " ", tr.get_text(" ", strip=True))
        if "441640" not in row_text:
            continue

        cells = [
            re.sub(r"\s+", " ", c.get_text(" ", strip=True))
            for c in tr.find_all(["td", "th"])
        ]
        full = " | ".join(cells)

        dates = re.findall(r"20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}", full)

        # Prefer explicit "NN원" values from the exact 441640 row.
        won_values = [
            int(x.replace(",", ""))
            for x in re.findall(r"(\d{1,5}(?:,\d{3})?)\s*원", full)
        ]
        won_values = [x for x in won_values if 10 <= x <= 5000]

        amount = won_values[0] if won_values else None
        record = parse_date_value(dates[0]) if len(dates) >= 1 else None
        payment = parse_date_value(dates[1]) if len(dates) >= 2 else None

        if amount:
            return {
                "amount": amount,
                "record_date": record,
                "payment_date": payment,
                "url": FUNDS["441640"]["official_distribution"],
                "source": "KODEX 공식 분배금 현황",
            }

    raise RuntimeError("KODEX 공식 분배금 현황에서 441640 행을 찾지 못했습니다.")


def parse_kodex_latest_notice():
    soup = get_soup(FUNDS["441640"]["official_search"])
    links = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        if "notice-view.do" in href and ("분배금" in title or "월중배당" in title):
            u = urljoin("https://www.samsungfund.com", href)
            if u not in links:
                links.append(u)

    for u in links[:30]:
        try:
            s = get_soup(u)
        except Exception:
            continue

        for tr in s.find_all("tr"):
            txt = re.sub(r"\s+", " ", tr.get_text(" ", strip=True))
            if "441640" not in txt:
                continue

            cells = [
                re.sub(r"\s+", " ", c.get_text(" ", strip=True))
                for c in tr.find_all(["td", "th"])
            ]

            # Read explicit won amount first.
            for cell in reversed(cells):
                m = re.search(r"(\d{2,4})\s*원", cell)
                if m:
                    amount = int(m.group(1))
                    if 10 <= amount <= 5000:
                        return {
                            "amount": amount,
                            "url": u,
                            "source": "KODEX 공식 분배금 공지",
                        }

    raise RuntimeError("최신 KODEX 공식 분배금 공지 파싱 실패")


# ---------------------------------------------------------------------
# TIGER official cross-check
# ---------------------------------------------------------------------

def parse_tiger_official_notice():
    root = get_soup(FUNDS["441680"]["official_notice_list"])
    links = []

    for a in root.find_all("a", href=True):
        href = a["href"]
        if "notice/view.do" in href and "detailsKey=" in href:
            u = urljoin("https://www.tigeretf.com", href)
            if u not in links:
                links.append(u)

    candidates = []

    for u in links[:50]:
        try:
            s = get_soup(u)
        except Exception:
            continue

        body = re.sub(r"\s+", " ", s.get_text(" ", strip=True))
        if "441680" not in body or "분배금" not in body:
            continue

        amount = None

        for tr in s.find_all("tr"):
            txt = re.sub(r"\s+", " ", tr.get_text(" ", strip=True))
            if "441680" not in txt:
                continue

            cells = [
                re.sub(r"\s+", " ", c.get_text(" ", strip=True))
                for c in tr.find_all(["td", "th"])
            ]

            # Use only explicit won amount from this exact row.
            for cell in cells:
                m = re.search(r"(\d{2,4})\s*원", cell)
                if m:
                    v = int(m.group(1))
                    if 10 <= v <= 5000:
                        amount = v
                        break

            if amount is not None:
                break

        if amount is None:
            continue

        pub_dates = re.findall(
            r"20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}",
            body
        )
        pub = parse_date_value(pub_dates[0]) if pub_dates else date.min

        candidates.append({
            "amount": amount,
            "publish_date": pub,
            "url": u,
            "source": "TIGER 공식 분배금 공지",
        })

    if not candidates:
        raise RuntimeError("TIGER 공식 공지를 읽지 못했습니다.")

    candidates.sort(key=lambda x: x["publish_date"], reverse=True)
    return candidates[0]


# ---------------------------------------------------------------------
# Merge / cross-check
# ---------------------------------------------------------------------

def build_441640():
    retro = parse_retro("441640")

    official_results = []
    official_errors = []

    try:
        official_results.append(parse_kodex_official_distribution())
    except Exception as e:
        official_errors.append(f"공식현황:{type(e).__name__}")

    try:
        official_results.append(parse_kodex_latest_notice())
    except Exception as e:
        official_errors.append(f"공식공지:{type(e).__name__}")

    matched_sources = [
        x["source"]
        for x in official_results
        if x.get("amount") == retro["amount"]
    ]

    official_url = None
    if matched_sources:
        for x in official_results:
            if x.get("amount") == retro["amount"]:
                official_url = x.get("url")
                break
    elif official_results:
        official_url = official_results[0].get("url")

    return {
        "code": "441640",
        "name": FUNDS["441640"]["name"],
        **retro,
        "amount_verified": bool(matched_sources),
        "official_sources_matched": matched_sources,
        "official_errors": official_errors,
        "official_url": official_url,
        "schedule_source": "ETF Retro 정형 일정",
    }


def build_441680():
    retro = parse_retro("441680")

    official = None
    official_errors = []

    try:
        official = parse_tiger_official_notice()
    except Exception as e:
        official_errors.append(f"TIGER공식:{type(e).__name__}")

    matched = bool(
        official
        and official.get("amount") == retro["amount"]
    )

    return {
        "code": "441680",
        "name": FUNDS["441680"]["name"],
        **retro,
        "amount_verified": matched,
        "official_sources_matched": (
            ["TIGER 공식 분배금 공지"] if matched else []
        ),
        "official_errors": official_errors,
        "official_url": official.get("url") if official else None,
        "schedule_source": "ETF Retro 정형 일정",
    }


def build_fund(code):
    if code == "441640":
        return build_441640()
    return build_441680()


# ---------------------------------------------------------------------
# State / notifications
# ---------------------------------------------------------------------

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
    return (
        f'{x["code"]}:'
        f'{x["latest_record_date"].isoformat()}:'
        f'{x["latest_payment_date"].isoformat()}:'
        f'{x["amount"]}:'
        f'{x["amount_verified"]}'
    )


def verification_text(x):
    if x["amount_verified"]:
        sources = ", ".join(x["official_sources_matched"])
        return f"✅ <b>분배금 공식 교차검증 완료</b> · {escape(sources)}"

    err = ", ".join(x.get("official_errors", []))
    if not err:
        err = "공식값 미확인 또는 불일치"

    return (
        "⚠️ <b>공식 자동검증 미완료</b>\n"
        f"분배금은 ETF Retro의 정확한 <b>주당</b> 열 사용 · {escape(err)}"
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
    if x.get("yield_pct") is not None:
        yield_line = f"📊 최근 분배율: {x['yield_pct']:.2f}%\n"

    schedule = ""
    if x["schedule_verified"]:
        schedule = (
            "\n📌 <b>다음 분배 일정</b>\n"
            f"🛒 마지막 매수일: <b>{fmt_date(x['last_buy_date'])}</b>\n"
            "⚠️ 위 날짜 장 마감 전 <b>실제 체결 완료</b> 필요\n"
            f"📉 분배락일: {fmt_date(x['ex_date'])}\n"
            f"📅 지급기준일: {fmt_date(x['next_record_date'])}\n"
            f"💳 지급 예정일: {fmt_date(x['next_payment_date'])}\n"
            f"ℹ️ 일정 출처: {escape(x['schedule_source'])}\n"
        )

    links = f'🔗 <a href="{x["retro_url"]}">분배 상세</a>'
    if x.get("official_url"):
        links += f' · <a href="{x["official_url"]}">공식 출처</a>'

    msg = (
        "💰 <b>ETF 분배금 업데이트</b>\n\n"
        f"<b>{escape(x['name'])} ({x['code']})</b>\n"
        f"💵 최근 실제 1주당 분배금: <b>{x['amount']:,}원</b>{change}\n"
        f"📆 최근 기준일: {fmt_date(x['latest_record_date'])}\n"
        f"💳 최근 지급일: {fmt_date(x['latest_payment_date'])}\n"
        f"{yield_line}"
        f"{schedule}\n"
        f"{verification_text(x)}\n"
        f"{links}"
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
        f"📅 지급기준일: {fmt_date(x['next_record_date'])}\n"
        f"💳 지급 예정일: {fmt_date(x['next_payment_date'])}\n"
        f"ℹ️ 일정 출처: {escape(x['schedule_source'])}"
    )

    send_telegram(msg)


def main():
    state = load_state()
    today = datetime.now(KST).date()
    send_initial = os.getenv("SEND_INITIAL", "false").lower() == "true"
    errors = []

    for code in FUNDS:
        try:
            x = build_fund(code)
        except Exception as e:
            errors.append(f"{code}: {type(e).__name__}: {e}")
            continue

        print(
            f"{code}: amount={x['amount']} "
            f"official_verified={x['amount_verified']} "
            f"latest_record={x['latest_record_date']} "
            f"last_buy={x['last_buy_date']} "
            f"next_record={x['next_record_date']}"
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
                "amount_verified": x["amount_verified"],
                "latest_record_date": x["latest_record_date"].isoformat(),
                "latest_payment_date": x["latest_payment_date"].isoformat(),
                "last_buy_date": x["last_buy_date"].isoformat() if x["last_buy_date"] else None,
                "ex_date": x["ex_date"].isoformat() if x["ex_date"] else None,
                "next_record_date": x["next_record_date"].isoformat() if x["next_record_date"] else None,
                "next_payment_date": x["next_payment_date"].isoformat() if x["next_payment_date"] else None,
                "schedule_source": x["schedule_source"],
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
