import os
import time
import requests

# Настройки Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5296533274")

# Параметры стратегии Истинного Пробоя (Long Only + Open Interest)
TIMEFRAME = "15"               # Таймфрейм M15
LOOKBACK_CANDLES = 48          # База консолидации: 48 свечей (12 часов)
MIN_BREAK_PCT = 0.8            # Тело должно закрепиться минимум на 0.8% выше уровня
MAX_BREAK_PCT = 4.0            # Защита от свечей-переростков
OI_INCREASE_PCT = 5.0          # OI на свече пробоя выше среднего за период минимум на 5%
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

def get_candles_and_oi(symbol: str):
    """Сбор свечей с расчетом CVD и подтягивание истории Открытого Интереса (OI)"""
    kline_url = "https://api.bybit.com/v5/market/kline"
    oi_url = "https://api.bybit.com/v5/market/open-interest"

    kline_params = {
        "category": "linear",
        "symbol": symbol,
        "interval": TIMEFRAME,
        "limit": LOOKBACK_CANDLES + 2
    }
    
    # Bybit intervalTime для OI: 5min, 15min, 30min, 1h, 4h, 1d
    oi_params = {
        "category": "linear",
        "symbol": symbol,
        "intervalTime": "15min",
        "limit": LOOKBACK_CANDLES + 2
    }

    try:
        kline_res = session.get(kline_url, params=kline_params, timeout=5).json()
        oi_res = session.get(oi_url, params=oi_params, timeout=5).json()

        if kline_res.get("retCode") != 0 or oi_res.get("retCode") != 0:
            return []

        raw_candles = list(reversed(kline_res["result"]["list"][1:LOOKBACK_CANDLES + 1]))
        raw_oi = {int(x["timestamp"]): float(x["openInterest"]) for x in oi_res["result"]["list"]}

        candles = []
        cum_delta = 0.0

        for k in raw_candles:
            candle_time = int(k[0])
            c_open = float(k[1])
            c_high = float(k[2])
            c_low = float(k[3])
            c_close = float(k[4])
            c_vol = float(k[5])

            c_range = c_high - c_low
            delta = (c_vol * ((c_close - c_open) / c_range)) if c_range > 0 else 0.0
            cum_delta += delta

            # Находим ближайший по времени показатель OI (или берем значение по точному таймстемпу)
            oi_val = raw_oi.get(candle_time)
            if oi_val is None:
                # Если точный таймстемп чуть сдвинут в API, ищем ближайший
                closest_ts = min(raw_oi.keys(), key=lambda t: abs(t - candle_time), default=None)
                oi_val = raw_oi[closest_ts] if closest_ts and abs(closest_ts - candle_time) <= 900_000 else 0.0

            candles.append({
                "time": candle_time,
                "open": c_open,
                "high": c_high,
                "low": c_low,
                "close": c_close,
                "volume": c_vol,
                "delta": delta,
                "cvd": cum_delta,
                "oi": oi_val
            })
        return candles
    except Exception:
        pass
    return []

def scan_real_breakout(symbol: str):
    candles = get_candles_and_oi(symbol)
    if not candles or len(candles) < LOOKBACK_CANDLES:
        return

    trigger = candles[-1]
    history = candles[:-1]

    # Проверяем, получены ли данные по OI
    if trigger["oi"] <= 0:
        return

    history_oi_vals = [c["oi"] for c in history if c["oi"] > 0]
    if not history_oi_vals:
        return

    avg_oi = sum(history_oi_vals) / len(history_oi_vals)
    range_high = max(c["high"] for c in history)
    max_history_cvd = max(c["cvd"] for c in history)

    c_range = trigger["high"] - trigger["low"]
    if c_range == 0:
        return

    # --- ИСТИННЫЙ ПРОБОЙ ВВЕРХ (LONG + РОСТ ОИ) ---
    # Условия:
    # 1. Свеча закрылась выше High базы
    # 2. Пробой от 0.8% до 4.0%
    # 3. Закрытие полнотелое зеленое (Close > Open)
    # 4. Верхняя тень <= 25% свечи
    # 5. OI на свече выше среднего OI за базу минимум на 5%
    # 6. CVD пробивает максимум (подтверждение агрессивных покупок)
    if trigger["close"] > range_high:
        break_pct = ((trigger["close"] - range_high) / range_high) * 100
        upper_wick = trigger["high"] - max(trigger["open"], trigger["close"])
        wick_ratio = upper_wick / c_range
        oi_change_pct = ((trigger["oi"] - avg_oi) / avg_oi) * 100

        if (MIN_BREAK_PCT <= break_pct <= MAX_BREAK_PCT and
            trigger["close"] > trigger["open"] and
            wick_ratio <= 0.25 and
            oi_change_pct >= OI_INCREASE_PCT and
            trigger["cvd"] > max_history_cvd):

            event_key = (symbol, trigger["time"], "REAL_BREAK_BULL_OI")
            if event_key not in notified_events:
                notified_events.add(event_key)
                send_tg(
                    f"🚀 <b>ИСТИННЫЙ ПРОБОЙ ХАЯ (LONG + НАБОР ОИ): {symbol}</b>\n\n"
                    f"• Пробитый уровень High: <code>{range_high}</code>\n"
                    f"• Закрытие свечи: <code>{trigger['close']}</code> (Закрепление: <code>+{break_pct:.2f}%</code>)\n"
                    f"• Верхняя тень: всего <code>{wick_ratio*100:.0f}%</code> (нет сопротивления)\n"
                    f"• <b>Приток ОИ:</b> <code>+{oi_change_pct:.2f}%</code> к среднему (набор позиций)\n"
                    f"• Текущий ОИ: <code>{trigger['oi']:,.0f}</code> | Средний: <code>{avg_oi:,.0f}</code>\n"
                    f"• <b>CVD рекорд:</b> кумулятивная дельта на максимуме\n\n"
                    f"💡 <i>Вход: на ретесте пробитого уровня {range_high} на M5 или по рынку. Стоп под {range_high}.</i>\n"
                    f"🔗 <a href='https://www.bybit.com/trade/usdt/{symbol}'>Bybit</a> | "
                    f"<a href='https://www.coinglass.com/tv/Bybit_{symbol}'>CoinGlass</a>"
                )

def wait_for_m15_close():
    now = time.time()
    interval = 15 * 60
    sleep_time = interval - (now % interval) + 3
    time.sleep(sleep_time)

def main():
    print("✓ Запуск лонг-скринера истинных пробоев (Breakout High + OI Surge)...")
    symbols = get_active_symbols()
    print(f"Отслеживается {len(symbols)} инструментов.")

    while True:
        try:
            wait_for_m15_close()
            print(f"[{time.strftime('%H:%M:%S')}] Свеча M15 закрылась. Поиск пробоев с притоком ОИ...")

            for s in symbols:
                scan_real_breakout(s)
                time.sleep(0.08)

            if len(notified_events) > 300:
                notified_events.clear()

        except Exception as e:
            print(f"Ошибка цикла: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
