#!/usr/bin/env python3
import json
import os
import socket
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT_DIR = Path(__file__).resolve().parent
MEMORY_PATH = ROOT_DIR / "memory.md"
REPORT_PATH = ROOT_DIR / "latest_report_nasdaq.json"
AGENT_TAG = "NASDAQ"
AGENT_TITLE = "Nasdaq Hourly Tracker"


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


def to_num(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def check_dns(hosts):
    for host in hosts:
        socket.gethostbyname(host)


def fetch_json(url: str):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "nasdaq-hourly-tracker"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def build_report():
    now = now_kst()
    symbols = "^IXIC,^GSPC,^DJI,^VIX"
    try:
        payload = fetch_json(f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbols}")
        rows = payload.get("quoteResponse", {}).get("result", [])
    except Exception as e:
        err = f"Yahoo quote 호출 실패: {e}"
        status = "API_FALLBACK"
        summary = "시세 조회 실패"
        text = "\n".join(build_header(now, status, summary) + ["- 오류: " + err])
        return {
            "generated_at": now,
            "agent": AGENT_TAG,
            "status": status,
            "summary": summary,
            "errors": [err],
            "text": text,
            "rows": [],
            "api_error": err,
        }

    if not rows:
        err = "Yahoo quote 결과 비어 있음"
        status = "API_FALLBACK"
        summary = "시세 결과 비어 있음"
        text = "\n".join(build_header(now, status, summary) + ["- 오류: " + err])
        return {
            "generated_at": now,
            "agent": AGENT_TAG,
            "status": status,
            "summary": summary,
            "errors": [err],
            "text": text,
            "rows": [],
            "api_error": err,
        }

    names = {
        "^IXIC": "NASDAQ",
        "^GSPC": "S&P500",
        "^DJI": "DOW",
        "^VIX": "VIX",
    }

    status = "OK"
    summary = "미국 주요 지수 4종 시세 수집 완료"
    lines = build_header(now, status, summary)
    normalized = []
    for row in rows:
        symbol = row.get("symbol", "")
        price = to_num(row.get("regularMarketPrice"), None)
        chg = to_num(row.get("regularMarketChange"), 0.0)
        chg_pct = to_num(row.get("regularMarketChangePercent"), 0.0)
        if price is None:
            continue
        arrow = "▲" if (chg or 0) >= 0 else "▼"
        line = f"- {names.get(symbol, symbol)}: {price:.2f} ({arrow} {chg:+.2f}, {chg_pct:+.2f}%)"
        lines.append(line)
        normalized.append(
            {
                "symbol": symbol,
                "name": names.get(symbol, symbol),
                "price": price,
                "change": chg,
                "change_percent": chg_pct,
            }
        )

    text = "\n".join(lines)
    return {
        "generated_at": now,
        "agent": AGENT_TAG,
        "status": status,
        "summary": summary,
        "errors": [],
        "text": text,
        "rows": normalized,
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
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN_NASDAQ", "").strip() or os.environ.get(
        "TELEGRAM_BOT_TOKEN", ""
    ).strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID_NASDAQ", "").strip() or os.environ.get(
        "TELEGRAM_CHAT_ID", ""
    ).strip()
    if not bot_token or not chat_id:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    try:
        check_dns(["query1.finance.yahoo.com", "api.telegram.org"])
    except Exception as e:
        append_memory(f"DNS 실패: {e}")
        raise SystemExit(f"DNS check failed: {e}")

    report = build_report()
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    if report.get("api_error"):
        append_memory(f"API 실패 fallback 실행: {report['api_error']}")

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
