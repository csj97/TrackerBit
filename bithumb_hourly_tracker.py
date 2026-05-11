#!/usr/bin/env python3
import json
import os
import socket
import statistics
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = "https://api.bithumb.com/public"
KST = timezone(timedelta(hours=9))
ROOT_DIR = Path(__file__).resolve().parent
MEMORY_PATH = ROOT_DIR / "memory.md"
REPORT_PATH = ROOT_DIR / "latest_report.json"
AGENT_TAG = "BITHUMB"
AGENT_TITLE = "Bithumb Hourly Tracker"
TOPN = 30
MIN_TRADE_VALUE_24H = 3_500_000_000
MIN_SCORE = 72
MIN_RR2 = 2.0
MARKET_CAUTION_MIN_SCORE = 82
MARKET_CAUTION_MIN_RR2 = 2.5


def get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "bithumb-hourly-tracker"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")


def append_memory(message: str):
    line = f"- [{now_kst()}] [{AGENT_TAG}] {message}\n"
    with open(MEMORY_PATH, "a", encoding="utf-8") as f:
        f.write(line)


def build_header(now: str, status: str, summary: str):
    return [
        f"[{AGENT_TITLE}] {now}",
        f"- 상태: {status}",
        f"- 요약: {summary}",
    ]


def check_dns(hosts):
    for host in hosts:
        socket.gethostbyname(host)


def to_float(x):
    return float(str(x).replace(",", ""))


def pct_change(now, prev):
    if prev == 0:
        return 0.0
    return (now - prev) / prev


def parse_candles(data):
    # bithumb: [ts, open, close, high, low, volume]
    arr = []
    for r in data:
        ts = int(r[0])
        o = to_float(r[1])
        c = to_float(r[2])
        h = to_float(r[3])
        l = to_float(r[4])
        v = to_float(r[5])
        arr.append((ts, o, h, l, c, v))
    arr.sort(key=lambda x: x[0])
    return arr


def ema(values, span):
    if len(values) < span:
        return None
    k = 2 / (span + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def levels(candles, price):
    highs = []
    lows = []
    for i in range(2, len(candles) - 2):
        h = candles[i][2]
        l = candles[i][3]
        if h >= max(candles[i - 2][2], candles[i - 1][2], candles[i + 1][2], candles[i + 2][2]):
            highs.append(h)
        if l <= min(candles[i - 2][3], candles[i - 1][3], candles[i + 1][3], candles[i + 2][3]):
            lows.append(l)

    support = max([x for x in lows if x < price], default=min(c[3] for c in candles[-120:]))
    above = sorted(set([x for x in highs if x > price]))
    resistance1 = above[0] if above else max(c[2] for c in candles[-120:])
    resistance2 = above[1] if len(above) > 1 else resistance1 * 1.1
    return support, resistance1, resistance2


def volume_profile(candles, price, lookback=120, bins=24):
    c = candles[-lookback:] if len(candles) >= lookback else candles
    lo = min(x[3] for x in c)
    hi = max(x[2] for x in c)
    if hi <= lo:
        return 1.0, False

    step = (hi - lo) / bins
    hist = [0.0] * bins
    for x in c:
        tp = (x[2] + x[3] + x[4]) / 3
        idx = min(bins - 1, max(0, int((tp - lo) / step)))
        hist[idx] += x[5]

    cur_idx = min(bins - 1, max(0, int((price - lo) / step)))
    above_ratio = sum(hist[cur_idx + 1 :]) / (sum(hist) + 1e-9)

    lim_idx = min(bins - 1, max(0, int((price * 1.12 - lo) / step)))
    near = hist[cur_idx + 1 : lim_idx + 1]
    p70 = sorted(hist)[int(0.7 * (bins - 1))]
    gap = all(x < p70 for x in near)
    return above_ratio, gap


def fib_zone(candles):
    sub = candles[-180:] if len(candles) >= 180 else candles
    highs = [x[2] for x in sub]
    hi = max(highs)
    hi_idx = highs.index(hi)
    pre = sub[: hi_idx + 1]
    if len(pre) < 20:
        return None
    lo = min(x[3] for x in pre)
    if hi <= lo:
        return None
    f50 = hi - 0.5 * (hi - lo)
    f618 = hi - 0.618 * (hi - lo)
    return f50, f618


def candle_signals(cd, c4):
    signals = []
    if len(c4) >= 4 and all(c4[-i][4] > c4[-i - 1][4] for i in [1, 2, 3]):
        signals.append("4H_3연속양봉")

    if len(cd) >= 2:
        p = cd[-2]
        c = cd[-1]
        if p[4] < p[1] and c[4] > c[1] and c[4] >= p[1] and c[1] <= p[4]:
            signals.append("일봉_강세장악")

    d = cd[-1]
    body = abs(d[4] - d[1])
    rng = max(d[2] - d[3], 1e-9)
    lower = min(d[1], d[4]) - d[3]
    if lower / rng > 0.5 and body / rng < 0.35 and d[4] >= d[1]:
        signals.append("일봉_핀바")

    return signals


def get_candles(symbol, interval):
    payload = get_json(f"{BASE}/candlestick/{symbol}_KRW/{interval}")
    if payload.get("status") != "0000":
        raise RuntimeError(f"candlestick API error: {symbol}/{interval}/status={payload.get('status')}")
    return parse_candles(payload["data"])


def analyze_market_regime(ticker_rows, topn=TOPN):
    btc_d = get_candles("BTC", "24h")
    btc_h4 = get_candles("BTC", "4h")
    if len(btc_d) < 420 or len(btc_h4) < 420:
        return {
            "status": "MARKET_BAD",
            "score": 0,
            "reasons": ["BTC 캔들 이력 부족"],
            "metrics": {},
        }

    btc_price = btc_h4[-1][4]
    d_close = [x[4] for x in btc_d]
    h4_close = [x[4] for x in btc_h4]

    h4_e50 = ema(h4_close, 50)
    h4_e200 = ema(h4_close, 200)
    h4_e400 = ema(h4_close, 400)
    d_e50 = ema(d_close, 50)
    d_e200 = ema(d_close, 200)
    d_e400 = ema(d_close, 400)
    if None in (h4_e50, h4_e200, h4_e400, d_e50, d_e200, d_e400):
        return {
            "status": "MARKET_BAD",
            "score": 0,
            "reasons": ["BTC EMA 계산 불가"],
            "metrics": {},
        }

    btc_h4_uptrend = h4_e50 > h4_e200 > h4_e400 and btc_price > h4_e50
    btc_d_uptrend = d_e50 > d_e200 > d_e400 and btc_price > d_e50
    btc_above_h4_e50 = btc_price > h4_e50
    btc_change_4h = pct_change(h4_close[-1], h4_close[-2])
    btc_change_24h = pct_change(h4_close[-1], h4_close[-7]) if len(h4_close) >= 7 else 0.0

    top_rows = ticker_rows[:topn]
    changes_24h = [x[3] for x in top_rows]
    up_ratio = sum(1 for x in changes_24h if x > 0) / max(len(changes_24h), 1)
    median_change = statistics.median(changes_24h) if changes_24h else 0.0

    bad_reasons = []
    if not btc_h4_uptrend:
        bad_reasons.append("BTC 4H 추세 약세")
    if not btc_d_uptrend:
        bad_reasons.append("BTC 일봉 추세 약세")
    if btc_change_24h <= -0.025:
        bad_reasons.append("BTC 24H 낙폭 과도")
    if up_ratio < 0.35:
        bad_reasons.append("상위 코인 상승 비율 35% 미만")
    if median_change <= -0.015:
        bad_reasons.append("상위 코인 24H 중앙값 -1.5% 이하")

    score = 0
    score += 25 if btc_h4_uptrend else 0
    score += 25 if btc_d_uptrend else 0
    score += 15 if btc_above_h4_e50 else 0
    score += 10 if btc_change_4h > -0.015 and btc_change_24h > -0.025 else 0
    score += 15 if up_ratio >= 0.5 else (8 if up_ratio >= 0.35 else 0)
    score += 10 if median_change > 0 else (5 if median_change > -0.015 else 0)

    if bad_reasons:
        status = "MARKET_BAD"
        reasons = bad_reasons
    elif score >= 80:
        status = "MARKET_OK"
        reasons = ["시장 조건 양호"]
    else:
        status = "MARKET_CAUTION"
        reasons = ["시장 강도 애매"]

    return {
        "status": status,
        "score": round(score, 1),
        "reasons": reasons,
        "metrics": {
            "btc_price": btc_price,
            "btc_h4_uptrend": btc_h4_uptrend,
            "btc_d_uptrend": btc_d_uptrend,
            "btc_above_h4_e50": btc_above_h4_e50,
            "btc_change_4h_pct": round(btc_change_4h * 100, 2),
            "btc_change_24h_pct": round(btc_change_24h * 100, 2),
            "top30_up_ratio": round(up_ratio, 3),
            "top30_median_24h_pct": round(median_change * 100, 2),
        },
    }


def analyze_symbol(symbol, trade_value_24h, price):
    cd = get_candles(symbol, "24h")
    c4 = get_candles(symbol, "4h")

    # reliability gate
    if len(cd) < 420 or len(c4) < 420:
        return {"symbol": symbol, "status": "EXCLUDE", "reason": "캔들 이력 부족"}

    cd_close = [x[4] for x in cd]
    c4_close = [x[4] for x in c4]

    e50 = ema(c4_close, 50)
    e200 = ema(c4_close, 200)
    e400 = ema(c4_close, 400)
    d50 = ema(cd_close, 50)
    d200 = ema(cd_close, 200)
    d400 = ema(cd_close, 400)
    if None in (e50, e200, e400, d50, d200, d400):
        return {"symbol": symbol, "status": "EXCLUDE", "reason": "EMA 계산 불가"}

    trend_h4 = e50 > e200 > e400 and price > e50
    trend_d = d50 > d200 > d400 and price > d50

    support, r1, r2 = levels(cd[-220:], price)
    near_support = (price - support) / price <= 0.05
    near_break = (r1 - price) / price <= 0.03

    up = [x[5] for x in c4[-24:] if x[4] > x[1]]
    dn = [x[5] for x in c4[-24:] if x[4] <= x[1]]
    vol_up = (statistics.mean(up) > statistics.mean(dn) * 1.1) if up and dn else False

    up8 = [x[5] for x in c4[-8:] if x[4] > x[1]]
    dn8 = [x[5] for x in c4[-8:] if x[4] <= x[1]]
    pullback_ok = (statistics.mean(dn8) < statistics.mean(up8) * 0.9) if up8 and dn8 else False

    avg20 = statistics.mean([x[5] for x in c4[-20:]])
    breakout = c4[-1][4] >= max(x[2] for x in c4[-21:-1]) * 0.997 and c4[-1][5] >= avg20 * 1.35

    overhead, gap = volume_profile(cd, price)
    fz = fib_zone(cd)
    fib_ok = False
    f50 = None
    f618 = None
    if fz:
        f50, f618 = fz
        fib_ok = price >= f618 * 0.99 and price <= f50 * 1.03

    signals = candle_signals(cd, c4)

    score = 0
    score += 20 if (vol_up and (pullback_ok or breakout)) else (10 if vol_up else 0)
    score += 20 if (near_support or near_break) else 0
    score += 15 if overhead < 0.45 else (8 if overhead < 0.58 else 0)
    score += 20 if (trend_h4 and trend_d) else (12 if trend_h4 else (6 if trend_d else 0))
    score += 15 if fib_ok else 0
    score += min(10, len(signals) * 4)

    entry = price
    stop = support * 0.98
    risk = max(entry - stop, 1e-9)
    rr1 = (r1 - entry) / risk
    rr2 = (r2 - entry) / risk

    reasons = []
    if not trend_h4:
        reasons.append("4H EMA 정배열 미완")
    if not (vol_up and (pullback_ok or breakout)):
        reasons.append("거래량 패턴 미흡")
    if not (near_support or near_break):
        reasons.append("진입 위치 불리")
    if overhead >= 0.58:
        reasons.append("상단 매물대 과다")
    if fz and not fib_ok:
        reasons.append("피보나치 핵심구간 이탈")
    if rr2 < 2.0:
        reasons.append("손익비 부족")

    passed = (
        trade_value_24h >= MIN_TRADE_VALUE_24H
        and trend_h4
        and (vol_up and (pullback_ok or breakout))
        and (near_support or near_break)
        and overhead < 0.58
        and rr2 >= MIN_RR2
        and score >= MIN_SCORE
    )

    return {
        "symbol": symbol,
        "status": "PASS" if passed else "EXCLUDE",
        "reason": "통과" if passed else ", ".join(reasons),
        "score": round(score, 1),
        "price": price,
        "support": support,
        "res1": r1,
        "res2": r2,
        "stop": stop,
        "rr1": round(rr1, 2),
        "rr2": round(rr2, 2),
        "trend": "상승" if (trend_h4 and trend_d) else ("횡보" if price > e200 else "하락"),
        "vol_up": vol_up,
        "pullback_ok": pullback_ok,
        "breakout": breakout,
        "overhead": round(overhead, 3),
        "gap": gap,
        "fib50": f50,
        "fib618": f618,
        "fib_ok": fib_ok,
        "signals": signals,
        "trade_value_24h": trade_value_24h,
    }


def build_report(topn=TOPN):
    now = now_kst()
    try:
        ticker = get_json(f"{BASE}/ticker/ALL_KRW")
    except Exception as e:
        err = f"Bithumb ticker 호출 실패: {e}"
        status = "API_FALLBACK"
        summary = "시세 API 호출 실패로 분석 불가"
        text = "\n".join(build_header(now, status, summary) + ["- 오류: " + err])
        return {
            "generated_at": now,
            "agent": AGENT_TAG,
            "status": status,
            "summary": summary,
            "errors": [err],
            "topn": topn,
            "market": None,
            "top3": [],
            "analyzed": [],
            "text": text,
            "should_send": False,
            "api_error": err,
        }

    if ticker.get("status") != "0000":
        err = f"Bithumb ticker API error(status={ticker.get('status')})"
        status = "API_FALLBACK"
        summary = "시세 API status 비정상으로 분석 불가"
        text = "\n".join(build_header(now, status, summary) + ["- 오류: " + err])
        return {
            "generated_at": now,
            "agent": AGENT_TAG,
            "status": status,
            "summary": summary,
            "errors": [err],
            "topn": topn,
            "market": None,
            "top3": [],
            "analyzed": [],
            "text": text,
            "should_send": False,
            "api_error": err,
        }

    rows = []
    for k, v in ticker["data"].items():
        if k == "date" or k in {"USDT", "STABLE"}:
            continue
        change_24h = to_float(v.get("fluctate_rate_24H", 0)) / 100
        rows.append((k, to_float(v["acc_trade_value_24H"]), to_float(v["closing_price"]), change_24h))

    rows.sort(key=lambda x: x[1], reverse=True)
    target = rows[:topn]

    try:
        market = analyze_market_regime(rows, topn=topn)
    except Exception as e:
        market = {
            "status": "MARKET_BAD",
            "score": 0,
            "reasons": [f"시장 상태 분석 실패: {e}"],
            "metrics": {},
        }

    analyzed = []
    for sym, tv, p, _change_24h in target:
        try:
            r = analyze_symbol(sym, tv, p)
            if r:
                analyzed.append(r)
            time.sleep(0.08)
        except Exception as e:
            analyzed.append({"symbol": sym, "status": "EXCLUDE", "reason": f"분석실패:{e}"})

    passed = [x for x in analyzed if x.get("status") == "PASS"]
    if market["status"] == "MARKET_CAUTION":
        passed = [
            x
            for x in passed
            if x.get("score", 0) >= MARKET_CAUTION_MIN_SCORE and x.get("rr2", 0) >= MARKET_CAUTION_MIN_RR2
        ]
    if market["status"] == "MARKET_BAD":
        passed = []

    passed.sort(key=lambda x: (x["score"], x["rr2"], x["trade_value_24h"]), reverse=True)
    top3 = passed[:3]

    excluded = [x for x in analyzed if x.get("status") == "EXCLUDE"]
    excluded.sort(key=lambda x: x.get("score", 0), reverse=True)

    status = "OK" if market["status"] != "MARKET_BAD" else "MARKET_BLOCKED"
    summary = f"시장 {market['status']}({market['score']}점) / PASS {len(top3)} / 대상 {topn}"
    header = build_header(now, status, summary) + [
        f"- 대상: KRW 거래대금 상위 {topn} (USDT/STABLE 제외)",
        "- 기준: 4H+일봉, 거래량/지지저항/매물대/EMA/피보나치/캔들, 즉시매수 가능성",
        "- 시장 기준: BTC 4H/일봉 추세, BTC 단기 낙폭, 상위 코인 상승 비율, 상위 코인 중앙 등락률",
        f"- 시장 판단: {market['status']} / {market['score']}점 / {', '.join(market['reasons'])}",
    ]
    metrics = market.get("metrics") or {}
    if metrics:
        header.extend(
            [
                f"- BTC: 4H {metrics.get('btc_change_4h_pct')}% / 24H {metrics.get('btc_change_24h_pct')}%",
                f"- 상위30 상승비율: {metrics.get('top30_up_ratio')} / 중앙등락률: {metrics.get('top30_median_24h_pct')}%",
            ]
        )

    if not top3:
        if market["status"] == "MARKET_BAD":
            body = [
                "",
                "결론: 시장 상태가 좋지 않아 추천 종목 없음",
                "판단: 지금은 억지 매수 후보를 만들지 않고 관망",
                "",
                "근접 후보(참고, 매수 금지):",
            ]
        else:
            body = [
                "",
                "결론: 지금 당장 매수 가능한 강력 추천 코인 없음",
                "사유: 손익비 또는 추세/위치 조건 미달",
                "",
                "근접 후보(참고, 매수 금지):",
            ]
        for r in excluded[:3]:
            body.append(
                f"- {r['symbol']}: 점수 {r.get('score','-')} / RR2 {r.get('rr2','-')} / 사유 {r.get('reason','-')}"
            )
        text = "\n".join(header + body)
    else:
        body = ["", "강력 추천 TOP3:"]
        for idx, r in enumerate(top3, 1):
            body.extend(
                [
                    f"{idx}) {r['symbol']} (신뢰도 {r['score']})",
                    f"   - 현재가 {r['price']:.8g} / 추세 {r['trend']}",
                    f"   - 진입 {r['price']:.8g} / 손절 {r['stop']:.8g}",
                    f"   - 목표1 {r['res1']:.8g} / 목표2 {r['res2']:.8g}",
                    f"   - RR1 {r['rr1']} / RR2 {r['rr2']}",
                    f"   - 근거: 거래량({'O' if r['vol_up'] else 'X'}), 눌림감소({'O' if r['pullback_ok'] else 'X'}), 돌파거래량({'O' if r['breakout'] else 'X'}), 매물부담({r['overhead']}), 피보({'O' if r['fib_ok'] else 'X'})",
                ]
            )

        text = "\n".join(header + body)

    payload = {
        "generated_at": now,
        "agent": AGENT_TAG,
        "status": status,
        "summary": summary,
        "errors": [],
        "topn": topn,
        "market": market,
        "top3": top3,
        "analyzed": analyzed,
        "text": text,
        "should_send": bool(top3),
    }
    return payload


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
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not bot_token or not chat_id:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    try:
        check_dns(["api.bithumb.com", "api.telegram.org"])
    except Exception as e:
        append_memory(f"DNS 실패: {e}")
        raise SystemExit(f"DNS check failed: {e}")

    report = build_report(topn=TOPN)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    if report.get("api_error"):
        append_memory(f"API 실패 fallback 실행: {report['api_error']}")

    if not report.get("should_send"):
        append_memory(f"Telegram 전송 생략: 추천 후보 없음 / {report.get('summary')}")
        print("SKIPPED", report["generated_at"], report.get("summary"))
        return

    try:
        resp = send_telegram(bot_token, chat_id, report["text"])
    except Exception as e:
        append_memory(f"Telegram 전송 실패: {e}")
        raise RuntimeError(f"telegram send failed: {e}")
    if not resp.get("ok"):
        append_memory(f"Telegram 응답 실패: {resp}")
        raise RuntimeError(f"telegram send failed: {resp}")

    append_memory("정상 실행 완료")
    print("SENT", report["generated_at"])


if __name__ == "__main__":
    main()
