import json
import time
from collections import deque, defaultdict
from dataclasses import dataclass
from typing import Optional, Dict

import requests
from websocket import WebSocketApp
import os
from dotenv import load_dotenv

# ===================== Настройки =====================
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
INTERVAL  = os.getenv("INTERVAL", "1m")
SMA_LEN   = int(os.getenv("SMA_LEN", "14"))

# Список символов: из .env SYMBOLS=BTCUSDT,ETHUSDT,...
symbols_env = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT")
SYMBOLS = [s.strip().upper() for s in symbols_env.split(",") if s.strip()]

# Комбинированный WebSocket-URL: streams=btc@kline_1m/eth@kline_1m/...
def _stream(sym: str) -> str:
    return f"{sym.lower()}@kline_{INTERVAL}"
COMBINED_URL = "wss://stream.binance.com:9443/stream?streams=" + "/".join(_stream(s) for s in SYMBOLS)

TG_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# ===================== Утилиты =======================
def send_telegram(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("⚠️ TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы")
        return
    try:
        r = requests.post(TG_URL, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
        if r.status_code != 200:
            print("❌ Telegram error:", r.text)
    except Exception as e:
        print("❌ Telegram exception:", e)

def now_utc_str(ts_ms: int) -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(ts_ms // 1000))

# Barchart-версия Williams A/D: step = sign(close - prev_close) * (high - low)
def wad_step(close: float, prev_close: float, high: float, low: float) -> float:
    if close > prev_close:
        return (high - low)
    elif close < prev_close:
        return -(high - low)
    return 0.0

@dataclass
class WadState:
    prev_close: Optional[float] = None
    wad: float = 0.0
    sma_buf: deque = None
    last_rel: Optional[int] = None  # 1 если WAD>SMA, -1 если WAD<SMA

    def __post_init__(self):
        if self.sma_buf is None:
            self.sma_buf = deque(maxlen=SMA_LEN)

# Состояния по каждому символу
states: Dict[str, WadState] = defaultdict(WadState)

# ===================== WebSocket callbacks =====================
def on_open(ws):
    print(f"✅ Connected to {COMBINED_URL}")
    # стартовый пинг в Telegram
    try:
        sym_list = ", ".join(SYMBOLS)
        send_telegram(f"🚀 Бот запущен. Слежу за: <b>{sym_list}</b>  TF: <b>{INTERVAL}</b>  SMA: <b>{SMA_LEN}</b>")
    except Exception as e:
        print("❌ Не удалось отправить стартовый пинг:", e)

def on_close(ws, code, msg):
    print("🔌 Disconnected:", code, msg)

def on_error(ws, err):
    print("❌ WS error:", err)

def on_message(ws, msg):
    """
    Combined stream формат:
    {
      "stream": "btcusdt@kline_1m",
      "data": { "e":"kline", "E":..., "s":"BTCUSDT", "k": { ... kline ... } }
    }
    """
    try:
        root = json.loads(msg)
        data = root.get("data") or root  # на всякий случай, если кто-то подаст одиночный формат
        k = data.get("k", {})
        sym = (data.get("s") or k.get("s") or "").upper()
        if not sym or sym not in SYMBOLS:
            return

        # работаем только по закрытию свечи
        if not k.get("x", False):
            return

        close = float(k["c"])
        high  = float(k["h"])
        low   = float(k["l"])
        ts    = int(k["T"])

        st = states[sym]
        if st.prev_close is None:
            st.prev_close = close
            return

        st.wad += wad_step(close, st.prev_close, high, low)
        st.prev_close = close

        st.sma_buf.append(st.wad)
        if len(st.sma_buf) < SMA_LEN:
            return
        sma_val = sum(st.sma_buf) / len(st.sma_buf)

        rel = 1 if st.wad > sma_val else -1
        if st.last_rel is not None and rel != st.last_rel:
            direction = "⬆️ WAD crosses UP" if rel == 1 else "⬇️ WAD crosses DOWN"
            text = (
                f"{direction}\n"
                f"<b>{sym}</b>  TF: <b>{INTERVAL}</b>\n"
                f"Close: <code>{close}</code>\n"
                f"WAD: <code>{round(st.wad, 2)}</code>   SMA({SMA_LEN}): <code>{round(sma_val, 2)}</code>\n"
                f"Time: <code>{now_utc_str(ts)} UTC</code>"
            )
            print(text.replace("<b>", "").replace("</b>", ""))
            send_telegram(text)

        st.last_rel = rel

    except Exception as e:
        print("❌ on_message exception:", e)

# ===================== Runner =====================
def main():
    while True:
        ws = WebSocketApp(COMBINED_URL, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
        try:
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except KeyboardInterrupt:
            send_telegram("🛑 Бот остановлен вручную.")
            break
        except Exception as e:
            print("⚠️ Reconnect in 5s:", e)
            time.sleep(5)

if __name__ == "__main__":
    print("Starting multi-symbol WAD watcher...")
    main()

