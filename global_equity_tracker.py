#!/usr/bin/env python3
import json
import os
import socket
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.cookiejar import CookieJar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ETZ = ZoneInfo("America/New_York")
UTC = timezone.utc

ROOT_DIR = Path(__file__).resolve().parent
MEMORY_PATH = ROOT_DIR / "memory.md"
REPORT_PATH = ROOT_DIR / "latest_report_global_equity.json"
YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
YAHOO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9,ko-KR;q=0.8",
    "Referer": "https://finance.yahoo.com/",
}
YAHOO_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
YAHOO_CRUMB = None


def now_kst_str() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")


def append_memory(message: str):
    with open(MEMORY_PATH, "a", encoding="utf-8") as f:
        f.write(f"- [{now_kst_str()}] [GLOBAL] {message}\n")


def check_dns(hosts):
    for host in hosts:
        socket.gethostbyname(host)


def _is_yahoo_url(url: str) -> bool:
    return any(h in url for h in YAHOO_HOSTS)


def _append_query(url: str, key: str, value: str) -> str:
    p = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    q.append((key, value))
    return urllib.parse.urlunparse((p.scheme, p.netloc, p.path, p.params, urllib.parse.urlencode(q), p.fragment))


def _prime_yahoo_session(timeout: int):
    req = urllib.request.Request("https://finance.yahoo.com/", headers=YAHOO_HEADERS)
    with YAHOO_OPENER.open(req, timeout=timeout):
        pass


def _get_yahoo_crumb(timeout: int) -> str:
    global YAHOO_CRUMB
    if YAHOO_CRUMB:
        return YAHOO_CRUMB
    _prime_yahoo_session(timeout)
    for crumb_url in (
        "https://query1.finance.yahoo.com/v1/test/getcrumb",
        "https://query2.finance.yahoo.com/v1/test/getcrumb",
    ):
        req = urllib.request.Request(crumb_url, headers=YAHOO_HEADERS)
        try:
            with YAHOO_OPENER.open(req, timeout=timeout) as r:
                crumb = r.read().decode("utf-8", errors="replace").strip()
            if crumb and "html" not in crumb.lower():
                YAHOO_CRUMB = crumb
                return crumb
        except Exception:
            continue
    raise RuntimeError("yahoo crumb acquisition failed")


def _fetch_yahoo_json(url: str, timeout: int = 20):
    global YAHOO_CRUMB
    candidates = [url]
    if "query1.finance.yahoo.com" in url:
        candidates.append(url.replace("query1.finance.yahoo.com", "query2.finance.yahoo.com", 1))

    last_err = None
    # 1st pass: plain request, 2nd pass: crumb-attached request after 401/403.
    for pass_idx in range(2):
        for target in candidates:
            req_url = target
            if pass_idx == 1:
                crumb = _get_yahoo_crumb(timeout)
                req_url = _append_query(target, "crumb", crumb)
            req = urllib.request.Request(req_url, headers=YAHOO_HEADERS)
            try:
                with YAHOO_OPENER.open(req, timeout=timeout) as r:
                    return json.load(r)
            except HTTPError as e:
                last_err = e
                if e.code in (401, 403):
                    YAHOO_CRUMB = None
                continue
            except URLError as e:
                last_err = e
                continue
    raise RuntimeError(f"Yahoo fetch failed: {last_err}")


def fetch_json(url: str, timeout: int = 20):
    if _is_yahoo_url(url):
        return _fetch_yahoo_json(url, timeout=timeout)
    req = urllib.request.Request(url, headers={"User-Agent": "global-equity-tracker"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fetch_text(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": "global-equity-tracker"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def diagnose_yahoo_access():
    test_url = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=%5EIXIC"
    try:
        payload = fetch_json(test_url, timeout=20)
        rows = payload.get("quoteResponse", {}).get("result", [])
        if rows:
            return True, "ok"
        return False, "empty-result"
    except Exception as e:
        return False, str(e)


def ema(values, span):
    if len(values) < span:
        return None
    k = 2 / (span + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        d = values[-i] - values[-i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = mean(gains)
    avg_loss = mean(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(ohlc, period=14):
    if len(ohlc) < period + 1:
        return None
    trs = []
    for i in range(1, period + 1):
        h, l, c_prev = ohlc[-i]["high"], ohlc[-i]["low"], ohlc[-i - 1]["close"]
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        trs.append(tr)
    return mean(trs)


def pct(a, b):
    if b == 0:
        return 0.0
    return (a - b) / b * 100


def parse_universe(env_key: str, default_csv: str):
    raw = os.getenv(env_key, default_csv).strip()
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def get_quote(symbols):
    encoded = urllib.parse.quote(",".join(symbols))
    try:
        payload = fetch_json(f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={encoded}")
        rows = payload.get("quoteResponse", {}).get("result", [])
        return {r.get("symbol"): r for r in rows}
    except Exception:
        return {}


def get_chart(symbol: str, interval: str, rng: str):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?interval={interval}&range={rng}"
    try:
        payload = fetch_json(url)
    except Exception:
        return []
    result = payload.get("chart", {}).get("result", [])
    if not result:
        return []
    result = result[0]
    q = result.get("indicators", {}).get("quote", [{}])[0]
    closes = q.get("close", [])
    opens = q.get("open", [])
    highs = q.get("high", [])
    lows = q.get("low", [])
    vols = q.get("volume", [])
    ts = result.get("timestamp", [])
    out = []
    for i in range(len(ts)):
        if i >= len(closes) or closes[i] is None:
            continue
        if i >= len(highs) or i >= len(lows) or i >= len(opens):
            continue
        if highs[i] is None or lows[i] is None or opens[i] is None:
            continue
        out.append(
            {
                "ts": ts[i],
                "open": float(opens[i]),
                "high": float(highs[i]),
                "low": float(lows[i]),
                "close": float(closes[i]),
                "volume": float(vols[i] or 0.0) if i < len(vols) else 0.0,
            }
        )
    return out


@dataclass
class Pick:
    symbol: str
    score: float
    entry: float
    stop: float
    target: float
    rr: float
    reason: str


def score_long_term(symbol: str, quote_row: dict):
    daily = get_chart(symbol, "1d", "2y")
    weekly = get_chart(symbol, "1wk", "5y")
    if len(daily) < 120 or len(weekly) < 80:
        return None
    dclose = [x["close"] for x in daily]
    wclose = [x["close"] for x in weekly]
    price = dclose[-1]

    w20 = ema(wclose, 20)
    w50 = ema(wclose, 50)
    d50 = ema(dclose, 50)
    d200 = ema(dclose, 200)
    if None in (w20, w50, d50, d200):
        return None

    high_52w = max(dclose[-252:]) if len(dclose) >= 252 else max(dclose)
    dd_from_high = pct(price, high_52w)
    rsi14 = rsi(dclose, 14)
    atr14 = atr(daily, 14)
    if rsi14 is None or atr14 is None:
        return None
    atr_pct = atr14 / price * 100 if price > 0 else 999

    market_cap = float(quote_row.get("marketCap") or 0)
    pe = quote_row.get("trailingPE")
    score = 0.0
    notes = []

    if w20 > w50:
        score += 28
        notes.append("주봉 추세 우상향")
    if price > w20:
        score += 18
    if d50 > d200:
        score += 16
    if -20 <= dd_from_high <= -3:
        score += 12
        notes.append("52주 고점 대비 건강한 조정")
    if atr_pct <= 4.5:
        score += 8
    if 45 <= rsi14 <= 68:
        score += 8
    if market_cap >= 10_000_000_000:
        score += 6
    if pe is not None and 8 <= float(pe) <= 40:
        score += 4

    stop = min(daily[-20:], key=lambda x: x["low"])["low"]
    risk = max(price - stop, 1e-9)
    target = price + risk * 2.2
    rr = (target - price) / risk

    reason = ", ".join(notes) if notes else "중립"
    return Pick(symbol=symbol, score=round(score, 1), entry=price, stop=stop, target=target, rr=round(rr, 2), reason=reason)


def score_swing(symbol: str, quote_row: dict):
    daily = get_chart(symbol, "1d", "1y")
    if len(daily) < 90:
        return None
    closes = [x["close"] for x in daily]
    vols = [x["volume"] for x in daily]
    price = closes[-1]

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    if None in (e20, e50):
        return None
    rsi14 = rsi(closes, 14)
    atr14 = atr(daily, 14)
    if rsi14 is None or atr14 is None:
        return None
    atr_pct = atr14 / price * 100 if price > 0 else 999
    high20 = max(closes[-20:])
    vol10 = mean(vols[-10:]) if len(vols) >= 10 else 0
    vol40 = mean(vols[-40:]) if len(vols) >= 40 else 0
    vol_ratio = (vol10 / vol40) if vol40 > 0 else 1.0

    score = 0.0
    notes = []
    if e20 > e50:
        score += 24
    if price > e20:
        score += 16
    if 45 <= rsi14 <= 66:
        score += 14
    if price >= high20 * 0.99:
        score += 22
        notes.append("20일 고점 돌파 근접")
    if vol_ratio >= 1.2:
        score += 12
    if 2.0 <= atr_pct <= 8.5:
        score += 12

    stop = price - atr14 * 1.2
    target = price + atr14 * 2.4
    risk = max(price - stop, 1e-9)
    rr = (target - price) / risk
    reason = ", ".join(notes) if notes else "추세/모멘텀 혼합"
    return Pick(symbol=symbol, score=round(score, 1), entry=price, stop=stop, target=target, rr=round(rr, 2), reason=reason)


def choose_top(picks, min_score, topn=3):
    valid = [p for p in picks if p and p.score >= min_score and p.rr >= 1.8]
    valid.sort(key=lambda x: (x.score, x.rr), reverse=True)
    return valid[:topn]


def detect_event(now_utc: datetime):
    now_kst = now_utc.astimezone(KST)
    now_et = now_utc.astimezone(ETZ)

    if now_kst.weekday() < 5 and now_kst.hour == 8 and now_kst.minute == 30:
        return "KR_PREOPEN", now_kst
    if now_kst.weekday() < 5 and now_kst.hour == 12 and now_kst.minute == 30:
        return "KR_MIDCHECK", now_kst
    if now_kst.weekday() < 5 and now_kst.hour == 16 and now_kst.minute == 0:
        return "KR_POSTCLOSE", now_kst
    if now_et.weekday() < 5 and now_et.hour == 9 and now_et.minute == 0:
        return "US_PREOPEN", now_et
    if now_et.weekday() < 5 and now_et.hour == 12 and now_et.minute == 0:
        return "US_MIDCHECK", now_et
    if now_et.weekday() < 5 and now_et.hour == 16 and now_et.minute == 30:
        return "US_POSTCLOSE", now_et
    if now_kst.weekday() == 5 and now_kst.hour == 10 and now_kst.minute == 0:
        return "WEEKLY_REVIEW", now_kst
    return None, now_kst


def get_macro_snapshot():
    symbols = ["^KS11", "^KQ11", "KRW=X", "^GSPC", "^IXIC", "^DJI", "^VIX", "^TNX", "DX-Y.NYB"]
    q = get_quote(symbols)

    def fmt(sym, name):
        row = q.get(sym, {})
        p = row.get("regularMarketPrice")
        c = row.get("regularMarketChange")
        cp = row.get("regularMarketChangePercent")
        if p is None or c is None or cp is None:
            return f"- {name}: 데이터 부족"
        arrow = "▲" if c >= 0 else "▼"
        return f"- {name}: {p:.2f} ({arrow} {c:+.2f}, {cp:+.2f}%)"

    lines = [
        "거시/시장 스냅샷",
        fmt("^KS11", "KOSPI"),
        fmt("^KQ11", "KOSDAQ"),
        fmt("KRW=X", "USD/KRW"),
        fmt("^GSPC", "S&P500"),
        fmt("^IXIC", "NASDAQ"),
        fmt("^DJI", "DOW"),
        fmt("^VIX", "VIX"),
        fmt("^TNX", "US10Y"),
        fmt("DX-Y.NYB", "DXY"),
    ]

    vix = (q.get("^VIX", {}) or {}).get("regularMarketPrice")
    tnx = (q.get("^TNX", {}) or {}).get("regularMarketPrice")
    regime = "중립"
    if vix is not None and tnx is not None:
        if vix < 17 and tnx < 4.7:
            regime = "리스크온"
        elif vix > 22 or tnx > 4.9:
            regime = "리스크오프"
    lines.append(f"- 현재 체제 판단: {regime}")
    if not q:
        lines.append("- 주의: 실시간 시세 조회 실패(대체/캐시 데이터 없이 생성)")
    return lines


def parse_rss_headlines(url: str, limit: int):
    text = fetch_text(url)
    root = ET.fromstring(text)
    items = []
    for item in root.findall(".//item"):
        title = item.findtext("title", default="").strip()
        if title:
            items.append(title)
        if len(items) >= limit:
            break
    return items


def get_issue_headlines():
    urls = [
        "https://news.google.com/rss/search?q=미국+금리+연준+정책&hl=ko&gl=KR&ceid=KR:ko",
        "https://news.google.com/rss/search?q=한국+정치+경제+정책&hl=ko&gl=KR&ceid=KR:ko",
        "https://news.google.com/rss/search?q=지정학+리스크+유가&hl=ko&gl=KR&ceid=KR:ko",
    ]
    out = ["정치/경제/이슈 헤드라인"]
    for u in urls:
        try:
            for h in parse_rss_headlines(u, 2):
                out.append(f"- {h}")
        except Exception:
            continue
    if len(out) == 1:
        out.append("- 주요 헤드라인 수집 실패")
    return out[:8]


def build_recommendations(region: str):
    if region == "KR":
        long_uni = parse_universe(
            "KR_LONG_UNIVERSE",
            "005930.KS,000660.KS,035420.KS,005380.KS,105560.KS,068270.KS",
        )
        swing_uni = parse_universe(
            "KR_SWING_UNIVERSE",
            "005930.KS,000660.KS,035420.KS,042700.KS,091990.KS,247540.KQ,356320.KQ",
        )
    else:
        long_uni = parse_universe(
            "US_LONG_UNIVERSE",
            "MSFT,AAPL,NVDA,AMZN,GOOGL,META,BRK-B,LLY,AVGO,JPM",
        )
        swing_uni = parse_universe(
            "US_SWING_UNIVERSE",
            "QQQ,SOXX,SMH,NVDA,AMD,META,TSLA,AMZN",
        )

    all_symbols = sorted(set(long_uni + swing_uni))
    quotes = get_quote(all_symbols)

    long_picks = [score_long_term(s, quotes.get(s, {})) for s in long_uni]
    swing_picks = [score_swing(s, quotes.get(s, {})) for s in swing_uni]
    return choose_top(long_picks, min_score=55), choose_top(swing_picks, min_score=58)


def picks_to_lines(title: str, picks):
    lines = [title]
    if not picks:
        lines.append("- 조건 충족 종목 없음(현금 비중 유지 권고)")
        return lines
    for i, p in enumerate(picks, 1):
        lines.append(
            f"{i}) {p.symbol} | 점수 {p.score} | 진입 {p.entry:.2f} | 손절 {p.stop:.2f} | 목표 {p.target:.2f} | RR {p.rr}"
        )
        lines.append(f"- 근거: {p.reason}")
    return lines


def build_message(event_type: str, event_dt: datetime):
    now = now_kst_str()
    macro = get_macro_snapshot()
    issues = get_issue_headlines()
    kr_long, kr_swing = build_recommendations("KR")
    us_long, us_swing = build_recommendations("US")

    title_map = {
        "KR_PREOPEN": "주중 국장 장전 30분 브리핑",
        "KR_MIDCHECK": "주중 국장 장중 중간점검 브리핑",
        "KR_POSTCLOSE": "주중 국장 장마감 30분 브리핑",
        "US_PREOPEN": "주중 미장 장전 30분 브리핑",
        "US_MIDCHECK": "주중 미장 장중 중간점검 브리핑",
        "US_POSTCLOSE": "주중 미장 장마감 30분 브리핑",
        "WEEKLY_REVIEW": "토요일 주간 시황 정리 및 다음주 준비",
    }
    title = title_map.get(event_type, "시장 브리핑")

    lines = [
        f"[글로벌 멀티마켓 트래커] {now}",
        f"- 이벤트: {title}",
        f"- 기준 시각: {event_dt.strftime('%Y-%m-%d %H:%M %Z')}",
        "",
    ]

    lines += macro + [""] + issues + [""]

    if event_type in ("KR_PREOPEN", "KR_MIDCHECK", "KR_POSTCLOSE"):
        lines += picks_to_lines("국장 중장기 추천", kr_long)
        lines += [""]
        lines += picks_to_lines("국장 스윙 추천", kr_swing)
        lines += [""]
        lines += picks_to_lines("미장 중장기 추천(참고)", us_long[:2])
        lines += [""]
        lines += picks_to_lines("미장 스윙 추천(참고)", us_swing[:2])
        if event_type == "KR_MIDCHECK":
            lines += [""]
            lines += [
                "장중 포인트",
                "- 오전 대비 KOSPI/KOSDAQ 강도와 환율 방향성 동시 확인",
                "- 거래대금 상위 종목 쏠림이 2차전지/반도체/바이오 중 어디인지 체크",
                "- 장후반 변동성 확대 대비 손절/익절 트리거 재확인",
            ]
    elif event_type in ("US_PREOPEN", "US_MIDCHECK", "US_POSTCLOSE"):
        lines += picks_to_lines("미장 중장기 추천", us_long)
        lines += [""]
        lines += picks_to_lines("미장 스윙 추천", us_swing)
        lines += [""]
        lines += picks_to_lines("국장 중장기 추천(참고)", kr_long[:2])
        lines += [""]
        lines += picks_to_lines("국장 스윙 추천(참고)", kr_swing[:2])
        if event_type == "US_MIDCHECK":
            lines += [""]
            lines += [
                "장중 포인트",
                "- 나스닥 상대강도와 VIX 동행 여부(상승+VIX상승이면 경계)",
                "- 미 국채금리(US10Y) 급등 시 성장주 추격매수 축소",
                "- 엔비디아/메가캡 쏠림 약화 여부로 장후반 회전 리스크 점검",
            ]
    else:
        lines += picks_to_lines("국장 중장기 추천", kr_long)
        lines += [""]
        lines += picks_to_lines("국장 스윙 추천", kr_swing)
        lines += [""]
        lines += picks_to_lines("미장 중장기 추천", us_long)
        lines += [""]
        lines += picks_to_lines("미장 스윙 추천", us_swing)
        lines += [""]
        lines += [
            "다음주 준비 체크리스트",
            "- FOMC/물가지표/고용지표 일정 사전 확인",
            "- 환율(USD/KRW) 급등 시 국장 성장주 비중 축소",
            "- VIX 급등(22+) 구간은 레버리지 비중 축소",
            "- 손절 규칙(포지션당 손실 상한) 재점검",
        ]

    lines += ["", "리스크 고지", "- 본 정보는 투자 참고용이며 최종 판단과 책임은 투자자 본인에게 있습니다."]
    return "\n".join(lines), {
        "event_type": event_type,
        "generated_at_kst": now,
        "macro_lines": macro,
        "issue_lines": issues,
    }


def send_telegram(bot_token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def main():
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN_EQUITY", "").strip() or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID_EQUITY", "").strip() or os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN(_EQUITY) or TELEGRAM_CHAT_ID(_EQUITY)")

    now_utc = datetime.now(UTC)
    force_event = os.getenv("FORCE_EVENT", "").strip().upper()
    valid_force_events = {
        "KR_PREOPEN",
        "KR_MIDCHECK",
        "KR_POSTCLOSE",
        "US_PREOPEN",
        "US_MIDCHECK",
        "US_POSTCLOSE",
        "WEEKLY_REVIEW",
    }
    if force_event:
        if force_event not in valid_force_events:
            raise SystemExit(f"Invalid FORCE_EVENT: {force_event}")
        event_type, event_dt = force_event, now_utc.astimezone(KST)
        append_memory(f"FORCE_EVENT 사용: {force_event}")
    else:
        event_type, event_dt = detect_event(now_utc)
    if not event_type:
        print("NO_EVENT_WINDOW")
        return

    try:
        check_dns(
            [
                "query1.finance.yahoo.com",
                "query2.finance.yahoo.com",
                "news.google.com",
                "api.telegram.org",
            ]
        )
    except Exception as e:
        append_memory(f"DNS 실패: {e}")
        raise SystemExit(f"DNS check failed: {e}")

    yahoo_ok, yahoo_reason = diagnose_yahoo_access()
    if not yahoo_ok:
        append_memory(f"Yahoo 접근 진단 실패: {yahoo_reason}")

    text, payload = build_message(event_type, event_dt)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump({"text": text, **payload}, f, ensure_ascii=False, indent=2)

    try:
        resp = send_telegram(bot_token, chat_id, text)
    except Exception as e:
        append_memory(f"Telegram 전송 실패: {e}")
        raise RuntimeError(f"telegram send failed: {e}")
    if not resp.get("ok"):
        append_memory(f"Telegram 응답 실패: {resp}")
        raise RuntimeError(f"telegram send failed: {resp}")

    append_memory(f"정상 실행 완료: {event_type}")
    print("SENT", event_type)


if __name__ == "__main__":
    main()
