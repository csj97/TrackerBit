#!/usr/bin/env python3
import csv
import io
import json
import os
import socket
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ROOT_DIR = Path(__file__).resolve().parent
MEMORY_PATH = ROOT_DIR / "memory.md"
REPORT_PATH = ROOT_DIR / "latest_report_macro_agent.json"


def now_kst_str() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")


def append_memory(message: str):
    with open(MEMORY_PATH, "a", encoding="utf-8") as f:
        f.write(f"- [{now_kst_str()}] [MACRO] {message}\n")


def check_dns(hosts):
    for host in hosts:
        socket.gethostbyname(host)


def fetch_text(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": "macro-morning-agent"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def fetch_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": "macro-morning-agent"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def parse_float(value):
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except Exception:
        return None


def pct(now_v, prev_v):
    if now_v is None or prev_v in (None, 0):
        return None
    return (now_v - prev_v) / prev_v * 100.0


def fetch_stooq_close(symbol: str):
    # Stooq CSV: Symbol,Date,Time,Open,High,Low,Close,Volume
    text = fetch_text(f"https://stooq.com/q/l/?s={urllib.parse.quote(symbol)}&f=sd2t2ohlcv&h&e=csv")
    rows = [x.strip() for x in text.splitlines() if x.strip()]
    if len(rows) < 2:
        raise RuntimeError(f"stooq no row: {symbol}")
    cols = [c.strip() for c in rows[1].split(",")]
    if not cols or cols[0].upper() == "N/D":
        raise RuntimeError(f"stooq N/D: {symbol}")
    close_v = parse_float(cols[6] if len(cols) > 6 else None)
    if close_v is None:
        raise RuntimeError(f"stooq close missing: {symbol}")
    return close_v


def fetch_fred_series_last_two(series_id: str):
    # Public CSV endpoint (no API key)
    text = fetch_text(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}")
    reader = csv.DictReader(io.StringIO(text))
    vals = []
    for row in reader:
        v = parse_float(row.get(series_id))
        if v is not None:
            vals.append(v)
    if not vals:
        raise RuntimeError(f"fred empty: {series_id}")
    if len(vals) == 1:
        return vals[-1], None
    return vals[-1], vals[-2]


def parse_rss_headlines(url: str, limit: int):
    text = fetch_text(url, timeout=20)
    root = ET.fromstring(text)
    items = []
    for item in root.findall(".//item"):
        t = (item.findtext("title", default="") or "").strip()
        if t:
            items.append(t)
        if len(items) >= limit:
            break
    return items


def get_headlines():
    feeds = [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://feeds.reuters.com/Reuters/worldNews",
        "https://news.google.com/rss/search?q=미국+금리+연준+물가&hl=ko&gl=KR&ceid=KR:ko",
        "https://news.google.com/rss/search?q=한국+정치+경제+정책&hl=ko&gl=KR&ceid=KR:ko",
        "https://news.google.com/rss/search?q=지정학+리스크+유가&hl=ko&gl=KR&ceid=KR:ko",
    ]
    out = []
    errs = []
    for u in feeds:
        try:
            out.extend(parse_rss_headlines(u, 2))
        except Exception as e:
            errs.append(f"{u}:{e}")
    return out[:10], errs[:3]


def classify_social_sentiment(headlines):
    text = " ".join(headlines).lower()
    risk_off_kw = ["war", "conflict", "tariff", "sanction", "recession", "crisis", "strike", "shutdown", "침체", "전쟁"]
    risk_on_kw = ["ai", "growth", "deal", "stimulus", "cut", "rally", "회복", "부양", "완화"]
    off = sum(1 for k in risk_off_kw if k in text)
    on = sum(1 for k in risk_on_kw if k in text)
    if off >= on + 2:
        return "위험회피 우위", off, on
    if on >= off + 2:
        return "위험선호 우위", off, on
    return "중립/혼조", off, on


@dataclass
class MacroState:
    phase: str
    liquidity_score: int
    rate_pressure: int
    sentiment: str


def infer_macro_phase(ind):
    cpi, cpi_prev = ind["cpi"], ind["cpi_prev"]
    ppi, ppi_prev = ind["ppi"], ind["ppi_prev"]
    fed, _ = ind["fedfunds"], None
    unemp, unemp_prev = ind["unemp"], ind["unemp_prev"]
    gdp, _ = ind["gdp"], None
    dxy = ind["dxy"]
    us10y = ind["us10y"]
    spx_chg = ind["spx_chg"]

    cpi_mom = (cpi - cpi_prev) if (cpi is not None and cpi_prev is not None) else None
    ppi_mom = (ppi - ppi_prev) if (ppi is not None and ppi_prev is not None) else None
    unemp_mom = (unemp - unemp_prev) if (unemp is not None and unemp_prev is not None) else None

    liquidity = 0
    rate_pressure = 0
    if fed is not None:
        rate_pressure += 2 if fed >= 4.5 else 1 if fed >= 3.0 else 0
        liquidity += -2 if fed >= 4.5 else -1 if fed >= 3.0 else 1
    if us10y is not None:
        rate_pressure += 2 if us10y >= 4.8 else 1 if us10y >= 4.3 else 0
        liquidity += -1 if us10y >= 4.6 else 0
    if dxy is not None:
        liquidity += -1 if dxy >= 104 else 1 if dxy <= 101 else 0
    if cpi_mom is not None and ppi_mom is not None:
        if cpi_mom > 0 and ppi_mom > 0:
            liquidity += -1
        elif cpi_mom < 0 and ppi_mom < 0:
            liquidity += 1
    if unemp_mom is not None:
        if unemp_mom > 0.2:
            liquidity += 1
    if spx_chg is not None:
        liquidity += 1 if spx_chg > 0.7 else -1 if spx_chg < -0.7 else 0

    if liquidity <= -3:
        phase = "긴축 후반/성장 둔화 구간"
    elif liquidity >= 3:
        phase = "회복/유동성 완화 구간"
    else:
        phase = "전환기(혼조)"

    sentiment = ind["social_sentiment"]
    return MacroState(phase=phase, liquidity_score=liquidity, rate_pressure=rate_pressure, sentiment=sentiment)


def build_analysis(data):
    ind = data["indicators"]
    mkt = data["market"]
    news = data["headlines"]
    state = infer_macro_phase(
        {
            "cpi": ind["CPI"]["latest"],
            "cpi_prev": ind["CPI"]["prev"],
            "ppi": ind["PPI"]["latest"],
            "ppi_prev": ind["PPI"]["prev"],
            "fedfunds": ind["FedFunds"]["latest"],
            "gdp": ind["GDP"]["latest"],
            "unemp": ind["Unemployment"]["latest"],
            "unemp_prev": ind["Unemployment"]["prev"],
            "dxy": mkt["DXY"]["price"],
            "us10y": mkt["US10Y"]["price"],
            "spx_chg": mkt["S&P500"]["change_pct"],
            "social_sentiment": data["social"]["label"],
        }
    )

    key_signals = []
    if ind["CPI"]["mom"] is not None:
        key_signals.append(f"CPI 최근 변화 {ind['CPI']['mom']:+.2f}")
    if ind["PPI"]["mom"] is not None:
        key_signals.append(f"PPI 최근 변화 {ind['PPI']['mom']:+.2f}")
    if ind["FedFunds"]["latest"] is not None:
        key_signals.append(f"기준금리 {ind['FedFunds']['latest']:.2f}")
    if mkt["US10Y"]["price"] is not None:
        key_signals.append(f"미10년물 {mkt['US10Y']['price']:.2f}")
    if mkt["DXY"]["price"] is not None:
        key_signals.append(f"달러지수 {mkt['DXY']['price']:.2f}")

    why_lines = [
        f"- 유동성 우선 판단: liquidity_score={state.liquidity_score} (높을수록 완화, 낮을수록 긴축).",
        f"- 금리 압력(rate_pressure={state.rate_pressure})이 높으면 밸류에이션 부담이 커져 성장자산 변동성이 확대됩니다.",
        f"- 사회/뉴스 심리: {state.sentiment}. 뉴스는 단기 변동성을 키우지만, 유동성/금리 방향이 중기 추세를 결정합니다.",
    ]

    asset_impact = [
        "- 주식: 금리 압력이 높으면 멀티플 확장 제한, 실적 가시성 높은 대형주 중심으로 상대강도 발생 가능.",
        "- 채권: 성장 둔화 신호가 강화되면 장기금리 하방 압력, 반대로 인플레이션 재가열이면 금리 상방 리스크.",
        "- 달러: 금리차/리스크오프 환경에서 강세, 위험선호 회복 시 완만한 약세 가능.",
        "- 크립토: 유동성 개선 신호에는 민감하게 반응하지만, 실질금리 상승 구간에서는 변동성 확대 가능.",
    ]

    next_flow = [
        "- 단기(1~2주): 인플레이션/고용 발표 전후로 방향성보다 변동성 관리가 중요.",
        "- 중기(1~2개월): 금리 경로(인하 기대 재형성 여부)가 위험자산 추세를 좌우.",
        "- 국내 영향: 환율(USD/KRW)과 미국 장기금리 방향이 코스피/코스닥 수급에 직접 연결.",
    ]

    base = "인플레이션 둔화는 이어지지만 속도는 완만, 위험자산은 박스권 내 업종 순환."
    bull = "물가 하향+고용 연착륙이 동시 확인되면 멀티플 확장과 위험선호 회복."
    bear = "물가 재상승 또는 지정학 충격으로 금리/달러 동반 상승 시 위험자산 조정 심화."

    # Meta thinking: self challenge and contradictions
    weak = []
    if ind["GDP"]["latest"] is None:
        weak.append("GDP 최신치 시차")
    if not news:
        weak.append("뉴스 소스 공백")
    if mkt["S&P500"]["change_pct"] is None:
        weak.append("주식 단기 모멘텀 미확인")
    contradictory = []
    if state.liquidity_score < 0 and data["social"]["label"] == "위험선호 우위":
        contradictory.append("유동성은 타이트하지만 심리는 리스크온")
    if state.liquidity_score > 0 and mkt["DXY"]["change_pct"] is not None and mkt["DXY"]["change_pct"] > 0.5:
        contradictory.append("완화 신호 대비 달러 강세 지속")

    confidence = 72
    if weak:
        confidence -= min(12, len(weak) * 4)
    if contradictory:
        confidence -= min(10, len(contradictory) * 5)
    confidence = max(45, min(90, confidence))

    term_explain = "유동성: 시장에 풀린 자금의 여유 정도. 유동성이 줄면 위험자산이 약해지기 쉽습니다."

    lines = [
        f"[매크로 AI 모닝 브리핑] {now_kst_str()}",
        "",
        "1. 핵심 요약 (3줄)",
        f"- 핵심 신호: {', '.join(key_signals[:4]) if key_signals else '핵심 지표 수집 제한'}",
        f"- 현재 국면: {state.phase}",
        "- 판단 원칙: 단일 뉴스보다 유동성→금리→심리 순으로 가중치 부여.",
        "",
        "2. 현재 시장 상태 (Macro Phase)",
        f"- {state.phase}",
        f"- 심리 상태: {state.sentiment}",
        f"- {term_explain}",
        "",
        "3. 주요 원인 분석 (Why)",
    ]
    lines.extend(why_lines)
    lines += [
        "",
        "4. 시장 영향 (Asset별 영향)",
    ]
    lines.extend(asset_impact)
    lines += [
        "",
        "5. 앞으로 흐름 (Next Flow)",
    ]
    lines.extend(next_flow)
    lines += [
        "",
        "6. 시나리오 (Base / Bull / Bear)",
        f"- Base: {base}",
        f"- Bull: {bull}",
        f"- Bear: {bear}",
        "",
        "7. 한 줄 인사이트",
        "- 뉴스의 방향보다 금리와 달러의 방향이 중기 수익률 분포를 먼저 결정합니다.",
        "",
        "Meta Check",
        f"- 약한 가정: {', '.join(weak) if weak else '크게 없음'}",
        f"- 상충 신호: {', '.join(contradictory) if contradictory else '뚜렷하지 않음'}",
        "",
        f"Confidence: {confidence}/100",
        f"Key Uncertainty: {', '.join((weak + contradictory)[:4]) if (weak or contradictory) else '정책/지정학 이벤트 쇼크'}",
        "",
        "주의: 본 분석은 정보 제공 목적이며 직접적인 매수/매도 조언이 아닙니다.",
    ]
    return "\n".join(lines), confidence, weak, contradictory


def build_data():
    indicators = {}
    for name, sid in [
        ("CPI", "CPIAUCSL"),
        ("PPI", "PPIACO"),
        ("FedFunds", "FEDFUNDS"),
        ("GDP", "GDPC1"),
        ("Unemployment", "UNRATE"),
    ]:
        try:
            latest, prev = fetch_fred_series_last_two(sid)
            indicators[name] = {
                "series": sid,
                "latest": latest,
                "prev": prev,
                "mom": (latest - prev) if (latest is not None and prev is not None) else None,
            }
        except Exception as e:
            indicators[name] = {"series": sid, "latest": None, "prev": None, "mom": None, "error": str(e)}

    market_defs = {
        "S&P500": "^spx",
        "NASDAQ": "^ndq",
        "US10Y": "us10y",
        "DXY": "usdidx",
    }
    market = {}
    for k, sym in market_defs.items():
        try:
            price = fetch_stooq_close(sym)
            market[k] = {"symbol": sym, "price": price, "change_pct": None}
        except Exception as e:
            market[k] = {"symbol": sym, "price": None, "change_pct": None, "error": str(e)}

    # Crypto from CoinGecko
    try:
        c = fetch_json("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true")
        b = (c.get("bitcoin") or {})
        market["BTC"] = {"price": parse_float(b.get("usd")), "change_pct": parse_float(b.get("usd_24h_change"))}
    except Exception as e:
        market["BTC"] = {"price": None, "change_pct": None, "error": str(e)}

    headlines, headline_errors = get_headlines()
    sentiment_label, off_n, on_n = classify_social_sentiment(headlines)
    social = {
        "label": sentiment_label,
        "risk_off_hits": off_n,
        "risk_on_hits": on_n,
    }

    return {
        "generated_at": now_kst_str(),
        "indicators": indicators,
        "market": market,
        "headlines": headlines,
        "headline_errors": headline_errors,
        "social": social,
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
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN_MACRO", "").strip() or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID_MACRO", "").strip() or os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN(_MACRO) or TELEGRAM_CHAT_ID(_MACRO)")

    check_dns(
        [
            "fred.stlouisfed.org",
            "stooq.com",
            "api.coingecko.com",
            "feeds.reuters.com",
            "news.google.com",
            "api.telegram.org",
        ]
    )

    data = build_data()
    text, confidence, weak, contradictory = build_analysis(data)
    payload = {
        "raw_data": data,
        "analysis_text": text,
        "confidence": confidence,
        "weak_assumptions": weak,
        "contradictory_signals": contradictory,
    }
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    resp = send_telegram(bot_token, chat_id, text)
    if not resp.get("ok"):
        append_memory(f"Telegram 응답 실패: {resp}")
        raise RuntimeError(f"telegram send failed: {resp}")

    append_memory(f"매크로 모닝 브리핑 전송 완료 (confidence={confidence})")
    print("SENT", data["generated_at"])


if __name__ == "__main__":
    main()
