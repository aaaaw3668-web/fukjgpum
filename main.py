import os
import time
import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5296533274")

TIMEFRAME = "15"          # Рабочий таймфрейм (15 минут)
LOOKBACK_CANDLES = 20     # Количество свечей для определения локального экстремума
VOLUME_MULTIPLIER = 1.3   # Объем на свече пробоя должен быть на 30% выше среднего
MIN_TURNOVER_24H = 10_000  # Фильтр ликвидности (от $10M оборота за сутки)

session = requests.Session()
notified_events = set()  # Защита от дублей: (symbol, candle_timestamp, direction)

def send_tg(text: str):
    if not TELEGRAM_BOT_TOKEN:
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        session.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Ошибка отправки Telegram: {e}")

def get_active_symbols():
    """Получаем список активных бессрочных фьючерсов с хорошей ликвидностью"""
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

def get_klines(symbol: str, limit: int = 25):
    """Запрашивает последние закрытые свечи"""
    url = "https://api.bybit.com/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": TIMEFRAME,
        "limit": limit
    }
    try:
        res = session.get(url, params=params, timeout=5).json()
        if res.get("retCode") == 0 and res["result"]["list"]:
            raw_list = res["result"]["list"]
            # Bybit возвращает от новых к старым. Срез [1:] берет только ЗАКРЫТЫЕ свечи (исключая текущую формирующуюся [0])
            candles = []
            for k in reversed(raw_list[1:limit]):
                candles.append({
                    "start": int(k[0]),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5])
                })
            return candles
    except Exception:
        pass
    return []

def check_false_breakout(symbol: str):
    candles = get_klines(symbol, limit=LOOKBACK_CANDLES + 2)
    if len(candles) < LOOKBACK_CANDLES:
        return

    # Последняя закрытая свеча
    trigger_candle = candles[-1]
    history = candles[:-1]

    # Локальные экстремумы предыдущих баров
    range_high = max(c["high"] for c in history)
    range_low = min(c["low"] for c in history)

    # Средний объем по истории
    avg_vol = sum(c["volume"] for c in history) / len(history)

    c_open = trigger_candle["open"]
    c_close = trigger_candle["close"]
    c_high = trigger_candle["high"]
    c_low = trigger_candle["low"]
    c_vol = trigger_candle["volume"]
    c_time = trigger_candle["start"]

    candle_range = c_high - c_low
    if candle_range == 0:
        return

    # --- СЦЕНАРИЙ 1: Медвежий ложный пробой (Свип High -> Потенциальный ШОРТ) ---
    if c_high > range_high and c_close < range_high:
        upper_wick = c_high - max(c_open, c_close)
        # Тень сверху должна составлять не менее 40% от всей свечи, объем выше среднего
        if (upper_wick / candle_range) >= 0.40 and c_vol >= avg_vol * VOLUME_MULTIPLIER:
            event_key = (symbol, c_time, "BEARISH")
            if event_key not in notified_events:
                notified_events.add(event_key)
                send_tg(
                    f"🪤 <b>ЛОЖНЫЙ ПРОБОЙ ХАЯ (SHORT): {symbol}</b>\n\n"
                    f"• Пробитый уровень High: <code>{range_high}</code>\n"
                    f"• Тень свечи (High): <code>{c_high}</code> (Свип: <code>{((c_high - range_high)/range_high)*100:+.2f}%</code>)\n"
                    f"• Закрытие свечи: <code>{c_close}</code> (уход под уровень)\n"
                    f"• Всплеск объема: <code>{c_vol/avg_vol:.1f}x</code> от среднего\n"
                    f"• ТФ: <code>M{TIMEFRAME}</code>\n\n"
                    f"💡 <i>Собрана ликвидность на стопах покупателей. Вход при подтверждении на M5.</i>\n"
                    f"🔗 <a href='https://www.bybit.com/trade/usdt/{symbol}'>Bybit</a> | "
                    f"<a href='https://www.coinglass.com/tv/Bybit_{symbol}'>CoinGlass TV</a>"
                )

    # --- СЦЕНАРИЙ 2: Бычий ложный пробой (Свип Low -> Потенциальный ЛОНГ) ---
    elif c_low < range_low and c_close > range_low:
        lower_wick = min(c_open, c_close) - c_low
        # Тень снизу должна составлять не менее 40% от всей свечи, объем выше среднего
        if (lower_wick / candle_range) >= 0.40 and c_vol >= avg_vol * VOLUME_MULTIPLIER:
            event_key = (symbol, c_time, "BULLISH")
            if event_key not in notified_events:
                notified_events.add(event_key)
                send_tg(
                    f"🚀 <b>ЛОЖНЫЙ ПРОБОЙ ЛОЯ (LONG): {symbol}</b>\n\n"
                    f"• Пробитый уровень Low: <code>{range_low}</code>\n"
                    f"• Тень свечи (Low): <code>{c_low}</code> (Свип: <code>{((c_low - range_low)/range_low)*100:+.2f}%</code>)\n"
                    f"• Закрытие свечи: <code>{c_close}</code> (возврат над уровень)\n"
                    f"• Всплеск объема: <code>{c_vol/avg_vol:.1f}x</code> от среднего\n"
                    f"• ТФ: <code>M{TIMEFRAME}</code>\n\n"
                    f"💡 <i>Собрана ликвидность на стопах продавцов. Вход при подтверждении на M5.</i>\n"
                    f"🔗 <a href='https://www.bybit.com/trade/usdt/{symbol}'>Bybit</a> | "
                    f"<a href='https://www.coinglass.com/tv/Bybit_{symbol}'>CoinGlass TV</a>"
                )

def main():
    print("✓ Запуск сканера ложного пробоя...")
    symbols = get_active_symbols()
    print(f"Отслеживается {len(symbols)} инструментов.")

    while True:
        try:
            for s in symbols:
                check_false_breakout(s)
                time.sleep(0.08)  # Лимит запросов Bybit REST API
            
            # Очистка старых событий раз в цикл, чтобы память не текла
            if len(notified_events) > 500:
                notified_events.clear()

            time.sleep(15)
        except Exception as e:
            print(f"Ошибка цикла: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
