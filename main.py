import os
import time
import requests

# Настройки Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5296533274")

# Параметры стратегии Истинного Пробоя (Short only)
TIMEFRAME = "15"               # Таймфрейм M15
LOOKBACK_CANDLES = 48          # База консолидации: 48 свечей (12 часов)
MIN_BREAK_PCT = 0.8            # Тело должно закрепиться минимум на 0.8% ниже уровня
MAX_BREAK_PCT = 4.0            # Если свеча улетела на 7-10%, заходить уже поздно (FOMO)
VOLUME_MULTIPLIER = 2.0        # Объем на свече пробоя должен быть минимум в 2 раза выше среднего
MIN_TURNOVER_24H = 15_000  # Фильтр ликвидности

session = requests.Session()
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
        print(f"Ошибка загрузки тикеров: {e}")
    return []

def get_candles_data(symbol: str):
    """Сбор свечей с расчетом дельты и накопительного CVD"""
    url = "https://api.bybit.com/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": TIMEFRAME,
        "limit": LOOKBACK_CANDLES + 2
    }
    try:
        res = session.get(url, params=params, timeout=5).json()
        if res.get("retCode") == 0 and res["result"]["list"]:
            raw_candles = list(reversed(res["result"]["list"][1:LOOKBACK_CANDLES + 1]))
            
            candles = []
            cum_delta = 0.0

            for k in raw_candles:
                c_open = float(k[1])
                c_high = float(k[2])
                c_low = float(k[3])
                c_close = float(k[4])
                c_vol = float(k[5])

                c_range = c_high - c_low
                delta = (c_vol * ((c_close - c_open) / c_range)) if c_range > 0 else 0.0
                cum_delta += delta

                candles.append({
                    "time": int(k[0]),
                    "open": c_open,
                    "high": c_high,
                    "low": c_low,
                    "close": c_close,
                    "volume": c_vol,
                    "delta": delta,
                    "cvd": cum_delta
                })
            return candles
    except Exception:
        pass
    return []

def scan_real_breakout(symbol: str):
    candles = get_candles_data(symbol)
    if not candles or len(candles) < LOOKBACK_CANDLES:
        return

    trigger = candles[-1]
    history = candles[:-1]

    range_low = min(c["low"] for c in history)
    min_history_cvd = min(c["cvd"] for c in history)
    avg_vol = sum(c["volume"] for c in history) / len(history)

    c_range = trigger["high"] - trigger["low"]
    if c_range == 0:
        return

    # --- ИСТИННЫЙ ПРОБОЙ ВНИЗ (SHORT MOMENTUM) ---
    # Условия:
    # 1. Свеча закрылась СТРОГО ниже уровня Low базы
    # 2. Пробой тела от 0.8% до 4.0%
    # 3. Закрытие полнотелое красное (Close < Open)
    # 4. Нижняя тень маленькая (<= 25% свечи) — покупатели не откупают
    # 5. Объем в 2x выше среднего
    # 6. CVD CONFIRMATION: Кумулятивная дельта пробила минимум базы
    if trigger["close"] < range_low:
        break_pct = ((range_low - trigger["close"]) / range_low) * 100
        lower_wick = min(trigger["open"], trigger["close"]) - trigger["low"]
        wick_ratio = lower_wick / c_range

        if (MIN_BREAK_PCT <= break_pct <= MAX_BREAK_PCT and
            trigger["close"] < trigger["open"] and
            wick_ratio <= 0.25 and
            trigger["volume"] >= avg_vol * VOLUME_MULTIPLIER and
            trigger["cvd"] < min_history_cvd):

            event_key = (symbol, trigger["time"], "REAL_BREAK_BEAR")
            if event_key not in notified_events:
                notified_events.add(event_key)
                send_tg(
                    f"💥 <b>ИСТИННЫЙ ПРОБОЙ ЛОЯ (SHORT): {symbol}</b>\n\n"
                    f"• Пробитый уровень Low: <code>{range_low}</code>\n"
                    f"• Закрытие свечи: <code>{trigger['close']}</code> (Закрепление: <code>-{break_pct:.2f}%</code>)\n"
                    f"• Нижняя тень: всего <code>{wick_ratio*100:.0f}%</code> (нет откупа покупателя)\n"
                    f"• Всплеск объёма: <code>{trigger['volume']/avg_vol:.1f}x</code> от среднего\n"
                    f"• <b>CVD рекорд:</b> шквал маркет-продаж пробил поддержки\n\n"
                    f"💡 <i>Вход: на ретесте пробитого уровня {range_low} на M5 или по рынку. Стоп над {range_low}.</i>\n"
                    f"🔗 <a href='https://www.bybit.com/trade/usdt/{symbol}'>Bybit</a> | "
                    f"<a href='https://www.coinglass.com/tv/Bybit_{symbol}'>CoinGlass</a>"
                )

def wait_for_m15_close():
    now = time.time()
    interval = 15 * 60
    sleep_time = interval - (now % interval) + 3
    time.sleep(sleep_time)

def main():
    print("✓ Запуск шорт-скринера истинных пробоев (Real Breakout Low + CVD)...")
    symbols = get_active_symbols()
    print(f"Отслеживается {len(symbols)} инструментов.")

    while True:
        try:
            wait_for_m15_close()
            print(f"[{time.strftime('%H:%M:%S')}] Свеча M15 закрылась. Проверка импульсов в шорт...")

            for s in symbols:
                scan_real_breakout(s)
                time.sleep(0.05)

            if len(notified_events) > 300:
                notified_events.clear()

        except Exception as e:
            print(f"Ошибка цикла: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
