import os
import time
import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5296533274")

TIMEFRAME = "15"               # Опрос по 15-минутным свечам
OI_FLUSH_THRESHOLD = -5.0      # Минимальный сброс OI за 15 мин (падение от -5% и ниже)
PRICE_DIP_MIN = -1.5           # Минимальный откат цены вниз (от -1.5%)
PRICE_DIP_MAX = -6.0           # Если цена упала сильнее -6%, это слом структуры, не лезем
MIN_TURNOVER_24H = 7_000_000  # Фильтр ликвидности (от $15M)

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

def get_market_data(symbol: str):
    """
    Запрашивает последние закрытые свечи и историю изменения открытого интереса (OI)
    """
    # 1. Запрос свечей цены
    kline_url = "https://api.bybit.com/v5/market/kline"
    k_params = {"category": "linear", "symbol": symbol, "interval": TIMEFRAME, "limit": 25}
    
    # 2. Запрос истории OI (интервал 15m)
    oi_url = "https://api.bybit.com/v5/market/open-interest"
    oi_params = {"category": "linear", "symbol": symbol, "intervalTime": "15min", "limit": 5}

    try:
        k_res = session.get(kline_url, params=k_params, timeout=5).json()
        oi_res = session.get(oi_url, params=oi_params, timeout=5).json()

        if k_res.get("retCode") != 0 or oi_res.get("retCode") != 0:
            return None

        k_list = k_res["result"]["list"]
        oi_list = oi_res["result"]["list"]

        if len(k_list) < 20 or len(oi_list) < 2:
            return None

        # Исключаем формирующуюся свечу [0], берем последнюю закрытую [1] и историю
        trigger_candle = k_list[1]
        candles_history = list(reversed(k_list[1:21]))

        # OI Bybit: список идет от новых к старым. [0] - текущий, [1] - 15 мин назад
        current_oi = float(oi_list[0]["openInterest"])
        prev_oi = float(oi_list[1]["openInterest"])

        return {
            "time": int(trigger_candle[0]),
            "open": float(trigger_candle[1]),
            "high": float(trigger_candle[2]),
            "low": float(trigger_candle[3]),
            "close": float(trigger_candle[4]),
            "current_oi": current_oi,
            "prev_oi": prev_oi,
            "history_closes": [float(c[4]) for c in candles_history]
        }
    except Exception:
        return None

def check_oi_flush(symbol: str):
    data = get_market_data(symbol)
    if not data:
        return

    # 1. Расчет изменения Открытого Интереса (OI) за 15 минут
    if data["prev_oi"] == 0:
        return
    oi_change_pct = ((data["current_oi"] - data["prev_oi"]) / data["prev_oi"]) * 100

    # 2. Расчет изменения цены на закрытой свече
    price_change_pct = ((data["close"] - data["open"]) / data["open"]) * 100

    # 3. Фильтр тренда (SMA 20 свечей): ищем сбросы лонгистов только в восходящем тренде
    sma_20 = sum(data["history_closes"]) / len(data["history_closes"])
    is_uptrend = data["close"] > sma_20

    # УСЛОВИЕ СЕТАПА:
    # - Восходящий тренд (цена выше средней за 5 часов)
    # - Резкий откат цены вниз (от -1.5% до -6.0%)
    # - Жесткий сброс позиций (OI упал сильнее чем на -5%)
    if is_uptrend and (PRICE_DIP_MAX <= price_change_pct <= PRICE_DIP_MIN) and (oi_change_pct <= OI_FLUSH_THRESHOLD):
        event_key = (symbol, data["time"], "OI_FLUSH_LONG")
        if event_key not in notified_events:
            notified_events.add(event_key)
            send_tg(
                f"🧹 <b>СБРОС ЛИКВИДАЦИЙ ПО ТРЕНДУ (LONG): {symbol}</b>\n\n"
                f"• Падение OI за 15м: <code>{oi_change_pct:.2f}%</code> (смыли плечи)\n"
                f"• Откат цены: <code>{price_change_pct:.2f}%</code>\n"
                f"• Текущая цена: <code>{data['close']}</code> (выше SMA-20: <code>{sma_20:.4f}</code>)\n"
                f"• ТФ: <code>M{TIMEFRAME}</code>\n\n"
                f"💡 <i>Лишние лонги ликвидированы. Ждем остановку падения на M5 для входа в продолжение тренда.</i>\n"
                f"🔗 <a href='https://www.bybit.com/trade/usdt/{symbol}'>Bybit</a> | "
                f"<a href='https://www.coinglass.com/tv/Bybit_{symbol}'>CoinGlass OI</a>"
            )

def wait_for_m15_close():
    now = time.time()
    interval = 15 * 60
    sleep_time = interval - (now % interval) + 3
    time.sleep(sleep_time)

def main():
    print("✓ Запуск скринера OI Flush (Ликвидационный сброс по тренду)...")
    symbols = get_active_symbols()
    print(f"Отслеживается {len(symbols)} инструментов.")

    while True:
        try:
            wait_for_m15_close()
            print(f"[{time.strftime('%H:%M:%S')}] Свеча закрылась. Сканирование сбросов OI...")

            for s in symbols:
                check_oi_flush(s)
                time.sleep(0.06)

            if len(notified_events) > 200:
                notified_events.clear()

        except Exception as e:
            print(f"Ошибка цикла: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
