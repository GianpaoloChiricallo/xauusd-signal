import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
from openai import OpenAI

SYMBOL = "XAU/USD"
INTERVAL = "15min"
OUTPUT_FILE = "levels.txt"
HISTORY_FILE = "levels_history.log"


def f(x):
    return float(x)


def ema(values, period):
    k = 2.0 / (period + 1.0)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1.0 - k)
    return e


def atr(bars, period=14):
    trs = []
    prev_close = None
    for b in bars:
        h, l, c = b["high"], b["low"], b["close"]
        if prev_close is None:
            tr = h - l
        else:
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        prev_close = c
    if len(trs) < period:
        return sum(trs) / max(len(trs), 1)
    return sum(trs[-period:]) / period


def parse_dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def fallback_line(now, bars, e20, e30, a14):
    last = bars[-1]["close"]
    recent8 = bars[-8:]
    recent24 = bars[-24:]
    s1 = min(b["low"] for b in recent8)
    s2 = min(b["low"] for b in recent24)
    r1 = max(b["high"] for b in recent8)
    r2 = max(b["high"] for b in recent24)
    up = 55 if last > e20 > e30 else 45 if last < e20 < e30 else 50
    down = 100 - up
    bias = "BULLISH" if up > down else "BEARISH" if down > up else "NEUTRAL"
    conf = "MEDIUM" if abs(up - down) >= 10 else "LOW"
    buy_trigger = r1 + 0.10 * a14
    sell_trigger = s1 - 0.10 * a14
    return (
        f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')};XAUUSD;STATUS=LEVELS_ONLY;"
        f"P_UP={up};P_DOWN={down};CONF={conf};BIAS={bias};"
        f"SUPPORT1={s1:.2f};SUPPORT2={s2:.2f};RESIST1={r1:.2f};RESIST2={r2:.2f};"
        f"TRIGGER_BUY={buy_trigger:.2f};TRIGGER_SELL={sell_trigger:.2f};"
        f"EMA20={e20:.2f};EMA30={e30:.2f};ATR14={a14:.2f};"
        "NOTE=Fallback tecnico automatico;DEMO_ONLY=1;SOURCE=TWELVE_DATA"
    )


def main():
    td_key = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
    oa_key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("OPENAI_MODEL", "gpt-5.6").strip() or "gpt-5.6"
    if not td_key or not oa_key:
        print("Missing TWELVE_DATA_API_KEY or OPENAI_API_KEY", file=sys.stderr)
        return 2

    resp = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "outputsize": 80,
            "timezone": "UTC",
            "apikey": td_key,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "error" or not data.get("values"):
        raise RuntimeError(f"Twelve Data error: {data}")

    now = datetime.now(timezone.utc)
    raw = []
    for row in data["values"]:
        dt = parse_dt(row["datetime"])
        # Use only completed M15 bars, with a 1-minute safety margin.
        if dt + timedelta(minutes=15) <= now - timedelta(minutes=1):
            raw.append({
                "datetime": dt,
                "open": f(row["open"]),
                "high": f(row["high"]),
                "low": f(row["low"]),
                "close": f(row["close"]),
            })

    bars = sorted(raw, key=lambda b: b["datetime"])
    if len(bars) < 35:
        raise RuntimeError(f"Not enough completed M15 bars: {len(bars)}")

    latest_age = now - (bars[-1]["datetime"] + timedelta(minutes=15))
    closes = [b["close"] for b in bars]
    e20 = ema(closes[-50:], 20)
    e30 = ema(closes[-60:], 30)
    a14 = atr(bars[-30:], 14)

    if latest_age > timedelta(minutes=45):
        line = (
            f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')};XAUUSD;STATUS=STALE;"
            "P_UP=50;P_DOWN=50;CONF=LOW;BIAS=NEUTRAL;"
            f"EMA20={e20:.2f};EMA30={e30:.2f};ATR14={a14:.2f};"
            "NOTE=Dati M15 non abbastanza recenti;DEMO_ONLY=1;SOURCE=TWELVE_DATA"
        )
    else:
        compact = [
            {
                "t": b["datetime"].strftime("%Y-%m-%d %H:%M"),
                "o": round(b["open"], 2),
                "h": round(b["high"], 2),
                "l": round(b["low"], 2),
                "c": round(b["close"], 2),
            }
            for b in bars[-32:]
        ]

        prompt = f"""
Sei un analista tecnico prudente di XAU/USD per SOLO CONTO DEMO.
Analizza esclusivamente i dati M15 forniti. Non generare un ordine, non scrivere ENTRY_CONFIRMED e non suggerire size.
Obiettivo: aggiornare livelli chiave per le prossime 1-2 ore.

Indicatori già calcolati:
ultimo_close={bars[-1]['close']:.2f}
EMA20={e20:.2f}
EMA30={e30:.2f}
ATR14={a14:.2f}

Ultime 32 candele M15 (UTC):
{compact}

Restituisci UNA SOLA RIGA, nessun markdown, esattamente con campi separati da punto e virgola:
TIMESTAMP_UTC;XAUUSD;STATUS=LEVELS_ONLY;P_UP=nn;P_DOWN=nn;CONF=LOW|MEDIUM|HIGH;BIAS=BULLISH|BEARISH|NEUTRAL;SUPPORT1=xx.xx;SUPPORT2=xx.xx;RESIST1=xx.xx;RESIST2=xx.xx;TRIGGER_BUY=xx.xx;TRIGGER_SELL=xx.xx;EMA20=xx.xx;EMA30=xx.xx;ATR14=xx.xx;NOTE=testo breve senza punto e virgola;DEMO_ONLY=1;SOURCE=TWELVE_DATA

Regole:
- P_UP + P_DOWN = 100.
- Le probabilità sono stime soggettive, non certezze.
- Supporti/resistenze devono derivare dalla struttura M15 visibile.
- TRIGGER_BUY deve stare sopra una resistenza rilevante; TRIGGER_SELL sotto un supporto rilevante.
- Se il quadro è ambiguo, usa CONF=LOW o MEDIUM e probabilità vicine a 50/50.
- Non inseguire prezzi estesi rispetto a EMA/ATR.
""".strip()

        try:
            client = OpenAI(api_key=oa_key)
            response = client.responses.create(model=model, input=prompt)
            line = response.output_text.strip().splitlines()[-1].strip()
            required = [
                ";XAUUSD;", "STATUS=LEVELS_ONLY", "P_UP=", "P_DOWN=", "CONF=",
                "BIAS=", "SUPPORT1=", "SUPPORT2=", "RESIST1=", "RESIST2=",
                "TRIGGER_BUY=", "TRIGGER_SELL=", "EMA20=", "EMA30=", "ATR14=",
                "DEMO_ONLY=1", "SOURCE=TWELVE_DATA",
            ]
            if not all(x in line for x in required):
                raise ValueError(f"Invalid model output: {line}")
            m_up = re.search(r"P_UP=(\d+)", line)
            m_down = re.search(r"P_DOWN=(\d+)", line)
            if not m_up or not m_down or int(m_up.group(1)) + int(m_down.group(1)) != 100:
                raise ValueError(f"Invalid probabilities: {line}")
        except Exception as exc:
            print(f"OpenAI analysis failed, using fallback: {exc}", file=sys.stderr)
            line = fallback_line(now, bars, e20, e30, a14)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        fh.write(line + "\n")
    with open(HISTORY_FILE, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")

    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
