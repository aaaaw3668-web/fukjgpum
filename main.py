import os
import time
import requests

# Настройки Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5296533274")

# Параметры M15 (Детект кульминации покупок)
LOOKBACK_M15 = 48             # База консолидации: 12 часов
MIN_BREAK_PCT_M15 = 0.8       # Пробой от 0.8%
MAX_BREAK_PCT_M15 = 5.0       # Верхний порог для адекватного соотношения риск/прибыль
VOL_MULT_M15 = 2.0            # Объем свечи M15 в 2 раза выше среднего
MIN_TURNOVER_24H = 10_000 # Фильтр суточного объема ($10M)

# Параметры M1 (Детект CHoCH)
M1_WATCH_EXPIRE_SEC = 45 * 60 # Сколько следить за монетой после M15 (45 минут)
M1_OI_MULT = 1.10             # Коэффициент превышения ОИ относительно среднего (например, +5% к среднему)

session = requests.Session()
# Список наблюдения: { symbol: {"pump_high": float, "break_level": float, "added_at": float} }
watchlist = {}
notified_events = set()

def send_tg(text: str):
    if not TELEGRAM_BOT_TOKEN:
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        session.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Ошибка Telegram: {e}")

def get_active_symbols():
    url = "https://api.bybit.com/v5/market/tickers?category=linear"
    try:
        res = session.get(url, timeout=10).json()
        if res.get("retCode") == 0:
            return [
                item["symbol"] for item in res["result"]["list"]
                if item["symbol"].endswith("USDT") and float(item.get("turnover24h", 0)) >= MIN_TURNOVER_24H
            ]
    except Exception as e:
        print(f"Ошибка тикеров: {e}")
    return []

def get_m15_data(symbol: str):
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "linear", "symbol": symbol, "interval": "15", "limit": LOOKBACK_M15 + 2}
    try:
        res = session.get(url, params=params, timeout=5).json()
        if res.get("retCode") == 0 and res["result"]["list"]:
            raw_candles = list(reversed(res["result"]["list"][1:LOOKBACK_M15 + 1]))
            candles = []
            cum_delta = 0.0
            for k in raw_candles:
                c_open, c_high, c_low, c_close, c_vol = map(float, k[1:6])
                c_range = c_high - c_low
                delta = (c_vol * ((c_close - c_open) / c_range)) if c_range > 0 else 0.0
                cum_delta += delta
                candles.append({
                    "time": int(k[0]), "open": c_open, "high": c_high,
                    "low": c_low, "close": c_close, "volume": c_vol, "cvd": cum_delta
                })
            return candles
    except Exception:
        pass
    return []

def get_m1_candles(symbol: str, limit: int = 20):
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "linear", "symbol": symbol, "interval": "1", "limit": limit + 1}
    try:
        res = session.get(url, params=params, timeout=5).json()
        if res.get("retCode") == 0 and res["result"]["list"]:
            raw = list(reversed(res["result"]["list"][1:limit + 1]))
            return [{
                "time": int(k[0]), "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])
            } for k in raw]
    except Exception:
        pass
    return []

def get_m1_oi(symbol: str, limit: int = 15):
    """Получает историю Open Interest по минутам"""
    url = "https://api.bybit.com/v5/market/open-interest"
    params = {"category": "linear", "symbol": symbol, "intervalTime": "5min", "limit": limit}
    try:
        res = session.get(url, params=params, timeout=5).json()
        if res.get("retCode") == 0 and res["result"]["list"]:
            raw = list(reversed(res["result"]["list"]))
            return [float(item["openInterest"]) for item in raw]
    except Exception:
        pass
    return []

def check_m15_candidates(symbols):
    """Шаг 1: Ищет импульсный пробой на M15 и заносит в Watchlist без отправки в TG"""
    print(f"[{time.strftime('%H:%M:%S')}] Свеча M15 закрылась. Поиск кандидатов на кульминацию...")
    for symbol in symbols:
        candles = get_m15_data(symbol)
        if not candles or len(candles) < LOOKBACK_M15:
            continue

        trigger = candles[-1]
        history = candles[:-1]
        range_high = max(c["high"] for c in history)
        max_history_cvd = max(c["cvd"] for c in history)
        avg_vol = sum(c["volume"] for c in history) / len(history)

        c_range = trigger["high"] - trigger["low"]
        if c_range == 0:
            continue

        if trigger["close"] > range_high:
            break_pct = ((trigger["close"] - range_high) / range_high) * 100
            upper_wick = trigger["high"] - max(trigger["open"], trigger["close"])
            wick_ratio = upper_wick / c_range

            if (MIN_BREAK_PCT_M15 <= break_pct <= MAX_BREAK_PCT_M15 and
                trigger["close"] > trigger["open"] and
                wick_ratio <= 0.25 and
                trigger["volume"] >= avg_vol * VOL_MULT_M15 and
                trigger["cvd"] > max_history_cvd):

                watchlist[symbol] = {
                    "pump_high": trigger["high"],
                    "break_level": range_high,
                    "added_at": time.time()
                }
                print(f"👀 {symbol} добавлен в Watchlist (High пампа: {trigger['high']})")
        time.sleep(0.04)

def check_m1_choch():
    """Шаг 2: Мониторит монеты из Watchlist на M1 в поисках CHoCH на повышенном ОИ"""
    now = time.time()
    symbols_to_remove = []

    for symbol, info in list(watchlist.items()):
        if now - info["added_at"] > M1_WATCH_EXPIRE_SEC:
            symbols_to_remove.append(symbol)
            continue

        m1_bars = get_m1_candles(symbol, limit=15)
        if not m1_bars or len(m1_bars) < 10:
            continue

        oi_data = get_m1_oi(symbol, limit=15)
        if not oi_data or len(oi_data) < 5:
            continue

        trigger_m1 = m1_bars[-1]
        history_m1 = m1_bars[:-1]

        if trigger_m1["high"] > info["pump_high"]:
            info["pump_high"] = trigger_m1["high"]

        swing_low = min(b["low"] for b in history_m1[-5:])
        
        # Расчет показателей ОИ
        curr_oi = oi_data[-1]
        history_oi = oi_data[:-1]
        avg_oi = sum(history_oi) / len(history_oi)

        # УСЛОВИЕ CHoCH НА M1 (на повышенном ОИ):
        # 1. Свеча закрылась КРАСНОЙ (Close < Open)
        # 2. Свеча телом закрылась НИЖЕ свингового минимума (пробой структуры)
        # 3. ОИ на свече слома выше среднего значения ОИ
        if (trigger_m1["close"] < swing_low and 
            trigger_m1["close"] < trigger_m1["open"] and 
            curr_oi >= avg_oi * M1_OI_MULT):

            event_key = (symbol, trigger_m1["time"], "M1_CHOCH")
            if event_key not in notified_events:
                notified_events.add(event_key)
                stop_loss = info["pump_high"]
                curr_price = trigger_m1["close"]
                stop_distance_pct = ((stop_loss - curr_price) / curr_price) * 100
                oi_ratio = curr_oi / avg_oi if avg_oi > 0 else 1.0

                send_tg(
                    f"⚡ <b>СЛОМ СТРУКТУРЫ (CHoCH M1) ПОСЛЕ ПАМПА: {symbol}</b>\n\n"
                    f"• Пробит свинговый Low (M1): <code>{swing_low}</code>\n"
                    f"• Закрытие M1 со сломом: <code>{curr_price}</code>\n"
                    f"• <b>ОИ на сломе:</b> <code>{oi_ratio:.2f}x</code> от среднего ОИ (Текущий: <code>{curr_oi:,.0f}</code>)\n"
                    f"• Пик пампа (Стоп-лосс): <code>{stop_loss}</code> (риск: <code>{stop_distance_pct:.2f}%</code>)\n"
                    f"• Цель (Тейк): <b>+2.0%</b> от текущей цены\n\n"
                    f"💡 <i>ММ разгрузился в пробойщиков M15. На M1 подтвержден перехват инициативы продавцами с набором ОИ.</i>\n"
                    f"🔗 <a href='https://www.bybit.com/trade/usdt/{symbol}'>Bybit</a> | "
                    f"<a href='https://www.coinglass.com/tv/Bybit_{symbol}'>CoinGlass</a>"
                )
                symbols_to_remove.append(symbol)

        time.sleep(0.05)

    for s in symbols_to_remove:
        watchlist.pop(s, None)

def main():
    print("✓ Запуск двухэтапного скринера (M15 Breakout + M1 CHoCH on OI)...")
    symbols = get_active_symbols()
    print(f"Отслеживается {len(symbols)} инструментов.")

    last_m15_checked = 0

    while True:
        try:
            now = time.time()

            # 1. Проверка M15 каждые 15 минут (:00, :15, :30, :45)
            if now - last_m15_checked >= 900 or (int(now) % 900 < 5 and now - last_m15_checked > 60):
                symbols = get_active_symbols()
                check_m15_candidates(symbols)
                last_m15_checked = now

            # 2. Проверка M1 для монет из Watchlist (каждые 15–20 секунд)
            if watchlist:
                check_m1_choch()

            if len(notified_events) > 200:
                notified_events.clear()

            time.sleep(15)

        except Exception as e:
            print(f"Ошибка главного цикла: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
