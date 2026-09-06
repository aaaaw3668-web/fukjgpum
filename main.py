import os
import time
import requests

# Настройки Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5296533274")

# Параметры стратегии
TIMEFRAME = "15"               # Рабочий таймфрейм свечей (15 минут)
LOOKBACK_CANDLES = 48          # База консолидации: 48 свечей (12 часов истории)
MIN_SWEEP_PCT = 0.8            # Минимальный прокол уровня тенью (от 0.8%)
MAX_SWEEP_PCT = 4.5            # Максимальный вынос (свыше 4.5% — неконтролируемый памп)
VOLUME_MULTIPLIER = 1.4        # Всплеск объема пробойной свечи относительно среднего
MIN_TURNOVER_24H = 10_000_000  # Фильтр ликвидности (от $15M оборота)

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
        print(f"Ошибка получения инструментов: {e}")
    return []

def get_candles_data(symbol: str):
    """Получает закрытые свечи и сразу считает внутрибарную дельту и накопительный CVD"""
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
            # Исключаем индекс 0 (текущая формирующаяся свеча)
            raw_candles = list(reversed(res["result"]["list"][1:LOOKBACK_CANDLES + 1]))
            
            candles = []
            cum_delta = 0.0

            for k in raw_candles:
                c_open = float(k[1])
                c_high = float(k[2])
                c_low = float(k[3])
                c_close = float(k[4])
                c_vol = float(k[5])

                # Внутрибарная дельта (Intrabar Volume Split)
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

def scan_combo_signal(symbol: str):
    candles = get_candles_data(symbol)
    if not candles or len(candles) < LOOKBACK_CANDLES:
        return

    # Последняя полностью закрытая свеча (триггер) и история базы
    trigger = candles[-1]
    history = candles[:-1]

    # 1. Уровни и средний объем базы
    range_high = max(c["high"] for c in history)
    range_low = min(c["low"] for c in history)
    max_history_cvd = max(c["cvd"] for c in history)
    min_history_cvd = min(c["cvd"] for c in history)
    avg_vol = sum(c["volume"] for c in history) / len(history)

    c_range = trigger["high"] - trigger["low"]
    if c_range == 0:
        return

    # --- СИТУАЦИЯ 1: МЕДВЕЖИЙ СЕТАП (ШОРТ) ---
    # Условия:
    # 1. Прокол High тенью от 0.8% до 4.5%
    # 2. Закрытие строго ниже пробитого уровня
    # 3. Свеча закрылась красной (Close < Open) с верхней тенью >= 45% длины
    # 4. Всплеск объема >= 1.4x
    # 5. CVD DIVERGENCE: CVD триггера НИЖЕ исторического пика базы (поглощение лимитками ММ)
    if trigger["high"] > range_high and trigger["close"] < range_high:
        sweep_pct = ((trigger["high"] - range_high) / range_high) * 100
        upper_wick = trigger["high"] - max(trigger["open"], trigger["close"])
        wick_ratio = upper_wick / c_range

        if (MIN_SWEEP_PCT <= sweep_pct <= MAX_SWEEP_PCT and
            wick_ratio >= 0.45 and
            trigger["close"] < trigger["open"] and
            trigger["volume"] >= avg_vol * VOLUME_MULTIPLIER and
            trigger["cvd"] < max_history_cvd):

            event_key = (symbol, trigger["time"], "BEAR_COMBO")
            if event_key not in notified_events:
                notified_events.add(event_key)
                send_tg(
                    f"🎯 <b>КОМБО СЕТАП: ВЫНОС ХАЯ + ДИВЕРГЕНЦИЯ CVD (SHORT)</b>\n\n"
                    f"🪙 <b>Монета:</b> <code>{symbol}</code>\n"
                    f"• Уровень High (12ч): <code>{range_high}</code>\n"
                    f"• Вынос тенью: <code>+{sweep_pct:.2f}%</code> (High: <code>{trigger['high']}</code>)\n"
                    f"• Возврат: свеча закрылась красной под уровнем\n"
                    f"• Доля верхней тени: <code>{wick_ratio*100:.0f}%</code>\n"
                    f"• Объём свечи: <code>{trigger['volume']/avg_vol:.1f}x</code> от среднего\n"
                    f"• <b>Подтверждение CVD:</b> кумулятивная дельта падает (крупный лимитный продавец)\n\n"
                    f"💡 <i>Вход при закреплении или ретесте на M5. Стоп за High выноса.</i>\n"
                    f"🔗 <a href='https://www.bybit.com/trade/usdt/{symbol}'>Bybit</a> | "
                    f"<a href='https://www.coinglass.com/tv/Bybit_{symbol}'>CoinGlass</a>"
                )

    # --- СИТУАЦИЯ 2: БЫЧИЙ СЕТАП (ЛОНГ) ---
    # Условия:
    # 1. Прокол Low тенью от 0.8% до 4.5%
    # 2. Закрытие строго выше пробитого уровня
    # 3. Свеча закрылась зеленой (Close > Open) с нижней тенью >= 45% длины
    # 4. Всплеск объема >= 1.4x
    # 5. CVD DIVERGENCE: CVD триггера ВЫШЕ исторического дна базы (лимитный откуп ММ)
    elif trigger["low"] < range_low and trigger["close"] > range_low:
        sweep_pct = ((range_low - trigger["low"]) / range_low) * 100
        lower_wick = min(trigger["open"], trigger["close"]) - trigger["low"]
        wick_ratio = lower_wick / c_range

        if (MIN_SWEEP_PCT <= sweep_pct <= MAX_SWEEP_PCT and
            wick_ratio >= 0.45 and
            trigger["close"] > trigger["open"] and
            trigger["volume"] >= avg_vol * VOLUME_MULTIPLIER and
            trigger["cvd"] > min_history_cvd):

            event_key = (symbol, trigger["time"], "BULL_COMBO")
            if event_key not in notified_events:
                notified_events.add(event_key)
                send_tg(
                    f"🎯 <b>КОМБО СЕТАП: ВЫНОС ЛОЯ + ДИВЕРГЕНЦИЯ CVD (LONG)</b>\n\n"
                    f"🪙 <b>Монета:</b> <code>{symbol}</code>\n"
                    f"• Уровень Low (12ч): <code>{range_low}</code>\n"
                    f"• Вынос тенью: <code>-{sweep_pct:.2f}%</code> (Low: <code>{trigger['low']}</code>)\n"
                    f"• Возврат: свеча закрылась зеленой над уровнем\n"
                    f"• Доля нижней тени: <code>{wick_ratio*100:.0f}%</code>\n"
                    f"• Объём свечи: <code>{trigger['volume']/avg_vol:.1f}x</code> от среднего\n"
                    f"• <b>Подтверждение CVD:</b> кумулятивная дельта выше минимума (лимитный выкуп)\n\n"
                    f"💡 <i>Вход при закреплении или ретесте на M5. Стоп за Low выноса.</i>\n"
                    f"🔗 <a href='https://www.bybit.com/trade/usdt/{symbol}'>Bybit</a> | "
                    f"<a href='https://www.coinglass.com/tv/Bybit_{symbol}'>CoinGlass</a>"
                )

def wait_for_m15_close():
    """Синхронизирует опрос со временем закрытия свечи M15 (:00, :15, :30, :45 + 3 сек)"""
    now = time.time()
    interval = 15 * 60
    sleep_time = interval - (now % interval) + 3
    time.sleep(sleep_time)

def main():
    print("✓ Запуск комбо-скринера (False Breakout + CVD Divergence)...")
    symbols = get_active_symbols()
    print(f"Отслеживается {len(symbols)} инструментов.")

    while True:
        try:
            wait_for_m15_close()
            print(f"[{time.strftime('%H:%M:%S')}] Свеча M15 закрылась. Проверка сетапов...")

            for s in symbols:
                scan_combo_signal(s)
                time.sleep(0.05)

            if len(notified_events) > 300:
                notified_events.clear()

        except Exception as e:
            print(f"Ошибка цикла: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
