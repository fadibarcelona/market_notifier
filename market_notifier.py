#!/usr/bin/env python3
"""
Gold & PSX Market Notifier  — v2  (KSE-100 fix)
Reads config from environment variables — safe for Railway deployment.

BUG FIXED:
  /timeseries/int/SYS    → system metric (~147), WRONG
  /timeseries/int/KSE100 → actual KSE-100 index (~115,000+), CORRECT
"""

import os
import smtplib
import requests
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def get_config():
    cfg = {
        "EMAIL_FROM":     os.environ.get("EMAIL_FROM", ""),
        "EMAIL_PASSWORD": os.environ.get("EMAIL_PASSWORD", ""),
        "EMAIL_TO":       os.environ.get("EMAIL_TO", ""),
        "SMTP_HOST":      os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        "SMTP_PORT":      int(os.environ.get("SMTP_PORT", "587")),
        "GOLD_API_KEY":   os.environ.get("GOLD_API_KEY", ""),
    }
    missing = [k for k in ["EMAIL_FROM", "EMAIL_PASSWORD", "EMAIL_TO", "GOLD_API_KEY"] if not cfg[k]]
    if missing:
        raise EnvironmentError(f"Missing env vars: {', '.join(missing)}")
    return cfg


def get_gold_price(api_key: str) -> dict:
    try:
        r = requests.get(
            "https://www.goldapi.io/api/XAU/USD",
            headers={"x-access-token": api_key, "Content-Type": "application/json"},
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        return {
            "usd_per_oz": round(d.get("price", 0), 2),
            "change_pct": round(d.get("price_change_percentage_24h", 0) or 0, 2),
            "open":       round(d.get("open_price", 0), 2),
            "high":       round(d.get("high_price", 0) or 0, 2),
            "low":        round(d.get("low_price",  0) or 0, 2),
        }
    except Exception as e:
        print(f"[WARN] Gold API error: {e}")
        return {"usd_per_oz": 0, "change_pct": 0, "open": 0, "high": 0, "low": 0}


def get_usd_pkr() -> float:
    try:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
        r.raise_for_status()
        return round(r.json()["rates"]["PKR"], 2)
    except Exception as e:
        print(f"[WARN] Exchange rate error: {e} — using fallback 278.5")
        return 278.5


def _psx_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://dps.psx.com.pk/",
        "Accept": "application/json, text/plain, */*",
    }

def _sane(v):
    return 50_000 < v < 500_000

def get_kse100() -> dict:
    # Layer 1: PSX intraday — correct symbol is KSE100 (NOT SYS)
    try:
        r = requests.get(
            "https://dps.psx.com.pk/timeseries/int/KSE100",
            headers=_psx_headers(), timeout=10,
        )
        r.raise_for_status()
        rows = r.json().get("data", [])
        if len(rows) >= 2:
            ts, price, vol = rows[0][0], float(rows[0][1]), rows[0][2]
            oldest_price   = float(rows[-1][1])
            current = round(price, 2)
            change  = round(current - oldest_price, 2)
            pct     = round((change / oldest_price) * 100, 2) if oldest_price else 0
            if _sane(current):
                ts_s = ts / 1000 if ts > 1e10 else ts
                return {
                    "current":  current, "change": change, "pct": pct,
                    "volume":   f"{int(vol):,}", "source": "PSX intraday",
                    "datetime": datetime.fromtimestamp(ts_s).strftime("%Y-%m-%d %H:%M"),
                }
            print(f"[WARN] PSX intraday sanity fail: got {current} — expected 50k-500k")
    except Exception as e:
        print(f"[WARN] PSX intraday error: {e}")

    # Layer 2: PSX end-of-day
    try:
        r = requests.get(
            "https://dps.psx.com.pk/timeseries/eod/KSE100",
            headers=_psx_headers(), timeout=10,
        )
        r.raise_for_status()
        rows = r.json().get("data", [])
        if len(rows) >= 2:
            close      = float(rows[-1][4])
            prev_close = float(rows[-2][4])
            change     = round(close - prev_close, 2)
            pct        = round((change / prev_close) * 100, 2) if prev_close else 0
            if _sane(close):
                return {
                    "current":  round(close, 2), "change": change, "pct": pct,
                    "volume":   f"{int(rows[-1][5]):,}" if rows[-1][5] else "N/A",
                    "source":   "PSX EOD", "datetime": str(rows[-1][0]),
                }
    except Exception as e:
        print(f"[WARN] PSX EOD error: {e}")

    # Layer 3: Yahoo Finance (15-min delay, no key needed)
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EKSE",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
        )
        r.raise_for_status()
        meta    = r.json()["chart"]["result"][0]["meta"]
        current = round(meta["regularMarketPrice"], 2)
        prev    = round(meta["previousClose"], 2)
        change  = round(current - prev, 2)
        pct     = round((change / prev) * 100, 2) if prev else 0
        if _sane(current):
            return {
                "current":  current, "change": change, "pct": pct,
                "volume":   f"{meta.get('regularMarketVolume', 0):,}",
                "source":   "Yahoo Finance (15min delay)",
                "datetime": datetime.fromtimestamp(meta.get("regularMarketTime", 0)).strftime("%Y-%m-%d %H:%M"),
            }
    except Exception as e:
        print(f"[WARN] Yahoo Finance error: {e}")

    print("[ERROR] All KSE-100 sources failed.")
    return {"current": 0, "change": 0, "pct": 0, "volume": "N/A", "source": "unavailable", "datetime": "N/A"}


def build_prediction(gold_usd, gold_pct, kse_pct):
    def bias(pct, hi=1.0, lo=-1.0):
        return "Bullish 📈" if pct > hi else ("Bearish 📉" if pct < lo else "Neutral ➡️")
    return {
        "gold_bias":    bias(gold_pct),
        "gold_support": round(gold_usd * 0.992, 2),
        "gold_resist":  round(gold_usd * 1.012, 2),
        "psx_bias":     bias(kse_pct, 0.5, -0.5),
        "note": "Rule-based estimates only. Not financial advice.",
    }


def build_html_email(gold, kse, usd_pkr, pred):
    TROY_TO_TOLA  = 11.6638 / 31.1035
    gold_pkr_tola = round(gold["usd_per_oz"] * usd_pkr * TROY_TO_TOLA)
    date_str      = datetime.now().strftime("%A, %d %B %Y — %I:%M %p")

    ga = "▲" if gold["change_pct"] >= 0 else "▼"
    gc = "#1D9E75" if gold["change_pct"] >= 0 else "#D85A30"
    ka = "▲" if kse["change"] >= 0 else "▼"
    kc = "#1D9E75" if kse["change"] >= 0 else "#D85A30"

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    body{{font-family:Arial,sans-serif;background:#f4f4f4;margin:0;padding:20px;color:#333}}
    .wrap{{max-width:600px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.09)}}
    .hdr{{background:#0F6E56;color:#fff;padding:24px 28px}}
    .hdr h1{{margin:0;font-size:20px;font-weight:700}}
    .hdr p{{margin:5px 0 0;font-size:13px;opacity:.8}}
    .bdy{{padding:24px 28px}}
    .grid{{display:table;width:100%;margin-bottom:24px}}
    .col{{display:table-cell;width:50%;padding-right:8px;vertical-align:top}}
    .col:last-child{{padding-right:0;padding-left:8px}}
    .card{{background:#f9f9f7;border-radius:8px;padding:16px;border:1px solid #e8e8e4}}
    .lbl{{font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px}}
    .val{{font-size:22px;font-weight:700;color:#111;margin-bottom:4px}}
    .sub{{font-size:13px;color:#555;margin-bottom:2px}}
    h2{{font-size:13px;font-weight:700;color:#555;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid #eee;padding-bottom:8px;margin:0 0 12px}}
    .sec{{margin-bottom:22px}}
    table{{width:100%;border-collapse:collapse;font-size:14px}}
    td{{padding:8px 6px;border-bottom:1px solid #f0f0ee}}
    td:first-child{{color:#666}}
    td:last-child{{font-weight:600;text-align:right;color:#111}}
    .ftr{{background:#f9f9f7;padding:14px 28px;font-size:11px;color:#aaa;border-top:1px solid #eee}}
    .note{{font-size:12px;color:#bbb;margin-top:10px}}
  </style>
</head>
<body>
<div class="wrap">
  <div class="hdr">
    <h1>📊 Gold &amp; PSX Daily Report</h1>
    <p>{date_str}</p>
  </div>
  <div class="bdy">
    <div class="grid">
      <div class="col">
        <div class="card">
          <div class="lbl">🟡 Gold / tola</div>
          <div class="val">PKR {gold_pkr_tola:,}</div>
          <div class="sub">USD {gold['usd_per_oz']:,} / oz</div>
          <div class="sub" style="color:{gc};font-weight:600">{ga} {abs(gold['change_pct'])}%</div>
        </div>
      </div>
      <div class="col">
        <div class="card">
          <div class="lbl">🏦 KSE-100</div>
          <div class="val">{kse['current']:,.2f}</div>
          <div class="sub" style="color:{kc};font-weight:600">{ka} {abs(kse['change']):,.2f} pts ({abs(kse['pct'])}%)</div>
          <div class="sub">Vol: {kse['volume']}</div>
          <div class="sub" style="color:#bbb;font-size:11px">via {kse.get('source','')}</div>
        </div>
      </div>
    </div>
    <div class="sec">
      <h2>🟡 Gold Details</h2>
      <table>
        <tr><td>USD / oz</td><td>${gold['usd_per_oz']:,}</td></tr>
        <tr><td>PKR / tola</td><td>PKR {gold_pkr_tola:,}</td></tr>
        <tr><td>Open</td><td>${gold['open']:,}</td></tr>
        <tr><td>High</td><td>${gold['high']:,}</td></tr>
        <tr><td>Low</td><td>${gold['low']:,}</td></tr>
        <tr><td>USD / PKR</td><td>{usd_pkr}</td></tr>
      </table>
    </div>
    <div class="sec">
      <h2>📈 Prediction — Next Session</h2>
      <table>
        <tr><td>Gold bias</td><td>{pred['gold_bias']}</td></tr>
        <tr><td>Gold support (USD/oz)</td><td>${pred['gold_support']:,}</td></tr>
        <tr><td>Gold resistance (USD/oz)</td><td>${pred['gold_resist']:,}</td></tr>
        <tr><td>KSE-100 bias</td><td>{pred['psx_bias']}</td></tr>
      </table>
      <p class="note">⚠️ {pred['note']}</p>
    </div>
  </div>
  <div class="ftr">Auto-generated · GoldAPI.io · dps.psx.com.pk · open.er-api.com · KSE source: {kse.get('source','N/A')} · as of {kse.get('datetime','N/A')}</div>
</div>
</body>
</html>"""


def send_email(subject, html_body, cfg):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg["EMAIL_FROM"]
    msg["To"]      = cfg["EMAIL_TO"]
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"]) as s:
        s.ehlo(); s.starttls(); s.ehlo()
        s.login(cfg["EMAIL_FROM"], cfg["EMAIL_PASSWORD"])
        s.sendmail(cfg["EMAIL_FROM"], cfg["EMAIL_TO"], msg.as_string())


def main():
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Starting market notifier...")
    cfg = get_config()

    gold    = get_gold_price(cfg["GOLD_API_KEY"])
    usd_pkr = get_usd_pkr()
    kse     = get_kse100()
    pred    = build_prediction(gold["usd_per_oz"], gold["change_pct"], kse["pct"])

    TROY_TO_TOLA  = 11.6638 / 31.1035
    gold_pkr_tola = round(gold["usd_per_oz"] * usd_pkr * TROY_TO_TOLA)

    print(f"  Gold   : ${gold['usd_per_oz']:,} / oz  →  PKR {gold_pkr_tola:,} / tola")
    print(f"  USD/PKR: {usd_pkr}")
    print(f"  KSE-100: {kse['current']:,.2f}  ({kse['pct']:+.2f}%)  source: {kse.get('source')}")

    subject = (
        f"📊 Market {datetime.now():%d %b %Y} — "
        f"Gold PKR {gold_pkr_tola:,} | KSE {kse['current']:,.0f}"
    )
    send_email(subject, build_html_email(gold, kse, usd_pkr, pred), cfg)
    print("  ✅ Email sent successfully.")


if __name__ == "__main__":
    main()
