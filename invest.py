"""
EMA DCA Playbook bot.

Runs daily (GitHub Actions). For every asset in config.yaml:
  Gap = (Harga - EMA) / EMA
  gap < low   -> Beli 2x    (Diskon besar)
  low .. 0%   -> Beli 1,5x  (Di bawah EMA)
  0% .. high  -> Beli 1x    (Normal)
  gap > high  -> Beli 0,5x  (Terlalu mahal, sisa ke cadangan)
Aturan aman: saham downtrend maks 1x · cadangan habis maks 1x ·
emas & kripto (budget 0) tahan · kripto +100% di atas EMA jual 20-25% ·
Januari rebalance.

Writes data.json (dashboard) and state.json (cadangan & riwayat).
Sends a Telegram report if TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are set.
Only the scheduled run on buy_day records the month; other runs are a watch.
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yaml

ROOT = Path(__file__).resolve().parent
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())
STATE_FILE, DATA_FILE = ROOT / "state.json", ROOT / "data.json"
TZ = ZoneInfo("Asia/Jakarta")
BULAN = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
         "Agustus", "September", "Oktober", "November", "Desember"]


# ------------------------------------------------------------------ data
BINANCE = ["https://data-api.binance.vision", "https://api.binance.com",
           "https://api1.binance.com", "https://api2.binance.com"]


def binance(symbol: str, tf: str) -> pd.DataFrame:
    for host in BINANCE:
        try:
            r = requests.get(f"{host}/api/v3/klines", timeout=20,
                             params={"symbol": symbol, "interval": tf, "limit": 1000})
            if r.status_code != 200:
                continue
            df = pd.DataFrame(r.json()).iloc[:, [0, 4, 6]]
            df.columns = ["t", "close", "close_time"]
            df = df[df.close_time < time.time() * 1000]        # closed candles only
            df["t"] = pd.to_datetime(df.t, unit="ms", utc=True)
            df["close"] = df.close.astype(float)
            return df[["t", "close"]].reset_index(drop=True)
        except requests.RequestException:
            continue
    raise RuntimeError("Binance tidak bisa diakses")


def yahoo(symbol: str, tf: str) -> pd.DataFrame:
    import yfinance as yf
    interval, period = {"1d": ("1d", "5y"), "1w": ("1wk", "15y")}[tf]
    raw = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=True)
    if raw is None or raw.empty:
        raise ValueError(f"Yahoo tidak punya data {symbol}")
    idx = raw.index
    now = pd.Timestamp.now(tz=idx.tz)
    keep = idx.normalize() < now.normalize() if tf == "1d" else idx + pd.Timedelta(days=7) <= now
    raw = raw[keep]
    return pd.DataFrame({"t": pd.to_datetime(raw.index, utc=True),
                         "close": raw["Close"].values}).dropna().reset_index(drop=True)


def fetch(symbol: str, cls: str, tf: str):
    """Returns (df, source). Crypto: Binance spot, fallback Yahoo XXX-USD."""
    if cls == "crypto":
        try:
            return binance(symbol, tf), "Binance Spot"
        except Exception as e:
            print(f"{symbol}: {e} → coba Yahoo")
            base = symbol[:-4] if symbol.endswith("USDT") else symbol
            return yahoo(f"{base}-USD", tf), "Yahoo (cadangan)"
    return yahoo(symbol, tf), "Yahoo Finance"


def usd_idr():
    try:
        df = yahoo("USDIDR=X", "1d")
        return float(df.close.iloc[-1])
    except Exception:
        return None


# ----------------------------------------------------------------- rules
def zone(gap, c):
    if gap < c["low"]:
        return 2.0, "Diskon besar", "diskon"
    if gap < 0:
        return 1.5, "Di bawah EMA", "bawah"
    if gap <= c["high"]:
        return 1.0, "Normal", "normal"
    return 0.5, "Terlalu mahal", "mahal"


def rp(x):
    return "Rp" + f"{x / 1e6:.1f}".replace(".", ",") + " jt"


def mx(m):
    return f"{m:g}".replace(".", ",") + "x"


def fmt(x):
    return f"{x:,.2f}" if x >= 100 else f"{x:,.4f}"


# ------------------------------------------------------------------ main
def main():
    now = datetime.now(TZ)
    is_buy_day = now.day == CFG["buy_day"]
    scheduled = os.getenv("EVENT_NAME") == "schedule"
    month_key = now.strftime("%Y-%m")
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    reserve = state.get("reserve", CFG["reserve_start_idr"])
    record = is_buy_day and scheduled and state.get("last_month") != month_key

    pot = CFG["pot_monthly_idr"]
    fx = usd_idr()
    assets, errors = [], []

    for cls, syms in (CFG.get("watchlist") or {}).items():
        c = CFG["classes"].get(cls)
        if not c or not syms:
            continue
        base = pot * c["budget_pct"] / 100 / len(syms)
        for sym in syms:
            sym = str(sym).upper()
            try:
                df, src = fetch(sym, cls, c["tf"])
                if len(df) < c["ema"] + 5:
                    raise ValueError(f"data baru {len(df)} candle")
                df["ema"] = df.close.ewm(span=c["ema"], adjust=False).mean()
                price, ema = float(df.close.iloc[-1]), float(df.ema.iloc[-1])
                gap = (price - ema) / ema * 100
                m, zlabel, zkey = zone(gap, c)
                notes = []
                lb = CFG.get("downtrend_lookback", 20)
                if c.get("downtrend_cap") and ema < df.ema.iloc[-1 - lb] and m > 1:
                    m = 1.0
                    notes.append("Saham downtrend → maks 1x")
                tp = c.get("take_profit_gap")
                if tp and gap >= tp:
                    notes.append(f"+{gap:.0f}% di atas EMA → jual 20–25%")
                tail = df.tail(160)
                assets.append({
                    "sym": sym, "cls": cls, "label": c["label"], "src": src,
                    "ema_len": c["ema"], "tf": c["tf"], "low": c["low"], "high": c["high"],
                    "price": price, "ema": ema, "gap": gap, "zone": zlabel, "zkey": zkey,
                    "mult": m, "base": base, "hold": base == 0, "notes": notes,
                    "date": df.t.iloc[-1].strftime("%Y-%m-%d"),
                    "series": [[r.t.strftime("%Y-%m-%d"), round(r.close, 6), round(r.ema, 6)]
                               for r in tail.itertuples()],
                })
                print(f"{sym}: gap {gap:+.1f}% → {zlabel}")
            except Exception as e:
                errors.append(f"{sym}: {str(e)[:80]}")
                print(f"{sym}: GAGAL {e}")

    # cadangan: tabungan dulu (0,5x), lalu ambil untuk 1,5–2x (paling diskon dulu)
    for a in assets:
        if a["base"] and a["mult"] < 1:
            reserve += a["base"] * (1 - a["mult"])
    for a in sorted([a for a in assets if a["base"] and a["mult"] > 1], key=lambda a: a["gap"]):
        extra = a["base"] * (a["mult"] - 1)
        if extra <= reserve:
            reserve -= extra
        else:
            a["mult"] = 1.0
            a["notes"].append("Cadangan tidak cukup → maks 1x")

    fixed = [{"label": f["label"], "amount": pot * f["budget_pct"] / 100} for f in CFG.get("fixed", [])]
    for a in assets:
        a["amount"] = a["base"] * a["mult"]
        if fx and not a["hold"]:
            if a["cls"] == "idx_stock":
                a["units"] = f"≈ {a['amount'] / (a['price'] * 100):.0f} lot"
            elif a["cls"] in ("us_etf", "us_stock"):
                a["units"] = f"≈ {a['amount'] / fx / a['price']:.2f} unit"
    total = sum(a["amount"] for a in assets) + sum(f["amount"] for f in fixed)

    # ---------------------------------------------------- state + history
    if record:
        state["reserve"] = round(reserve)
        state["last_month"] = month_key
        state["spent"] = round(state.get("spent", 0) + total)
        state["months_done"] = state.get("months_done", 0) + 1
        state.setdefault("history", []).append({
            "month": month_key, "total": round(total),
            "assets": {a["sym"]: a["mult"] for a in assets if not a["hold"]}})
        STATE_FILE.write_text(json.dumps(state, indent=1))

    data = {
        "updated": now.isoformat(), "date": f"{now.day} {BULAN[now.month - 1]} {now.year}",
        "is_buy_day": is_buy_day, "recorded": record, "buy_day": CFG["buy_day"],
        "pot_monthly": pot, "pot_total": CFG["pot_total_idr"], "months": CFG["months"],
        "months_done": state.get("months_done", 0), "spent": state.get("spent", 0),
        "reserve": reserve, "total": total, "fixed": fixed, "usd_idr": fx,
        "history": state.get("history", [])[-36:], "assets": assets, "errors": errors,
        "classes": CFG["classes"], "fixed_cfg": CFG.get("fixed", []),
    }
    DATA_FILE.write_text(json.dumps(data, separators=(",", ":")))

    # ------------------------------------------------------- telegram
    head = (f"📊 <b>EMA DCA · HARI BELI · {data['date']}</b>" if is_buy_day else
            f"👀 <b>EMA DCA · {data['date']}</b>\n<i>Pantauan harian — beli tanggal {CFG['buy_day']}</i>")
    out = [head, ""]
    icon = {"diskon": "🟩", "bawah": "🟦", "normal": "🟨", "mahal": "🟥"}
    for a in assets:
        act = "Tahan" if a["hold"] else f"Beli <b>{mx(a['mult'])}</b> = {rp(a['amount'])}"
        out.append(f"{icon[a['zkey']]} <b>{a['sym']}</b> gap {a['gap']:+.1f}% · {a['zone']} → {act}")
        out += [f"    ⚠️ {n}" for n in a["notes"]]
    out += [f"▫️ {f['label']} → {rp(f['amount'])} (tetap)" for f in fixed]
    out += ["", f"💰 Total: <b>{rp(total)}</b> · 🏦 Cadangan: <b>{rp(reserve)}</b>"]
    if is_buy_day and now.month == 1:
        out.append("🔄 Januari: rebalance ke target (jual yang > target +5 poin)")
    if errors:
        out.append("⚠️ Gagal: " + "; ".join(errors))
    if os.getenv("PAGES_URL"):
        out.append(f"\n🔗 {os.getenv('PAGES_URL')}")
    send("\n".join(out))


def send(text):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print(text)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20,
                      json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True}).raise_for_status()
    except Exception as e:
        print(f"Telegram gagal: {e}")


if __name__ == "__main__":
    main()
