import os
import time
import statistics
import requests

# --- Настройки Telegram ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5296533274")

# --- Параметры стратегии: Ложный пробой на сбросе OI ---
TIMEFRAME = "15"               # Таймфрейм свечей и OI (15m)
LOOKBACK_CANDLES = 48          # Консолидация: 48 свечей (12 часов)
MIN_BREAK_PCT = 3.0            # Минимальный вынос за хай: 3%
VOLUME_MULTIPLIER = 2.0        # Объем свечи в USDT >= 2.0x от медианы консолидации
MIN_Z_SCORE = 1.8              # Статистический выброс объема (Z >= 1.8)
MIN_OI_DROP_PCT = 4.0          # Минимальное падение открытого интереса: -4.0% за свечу
MIN_TURNOVER_24H = 100_000  # Фильтр ликвидности: от 100 тыс за 24 часа

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
    """Сбор закрытых свечей с ценами и оборотом в USDT."""
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
            # Пропускаем текущую незакрытую свечу (индекс 0)
            raw_candles = list(reversed(res["result"]["list"][1:LOOKBACK_CANDLES + 1]))
            candles = []
            for k in raw_candles:
                candles.append({
                    "time": int(k[0]),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "turnover": float(k[6])
                })
            return candles
    except Exception:
        pass
    return []

def get_oi_change(symbol: str, candle_start_time: int):
    """
    Запрашивает историю Open Interest с Bybit API
    и вычисляет процентное изменение за интервал сигнальной свечи.
    """
    url = "https://api.bybit.com/v5/market/open-interest"
    # Интервал 15m для Open Interest в API Bybit передается как '15min'
    interval_str = "15min" if TIMEFRAME == "15" else f"{TIMEFRAME}min"
    
    # Запрашиваем 3 последние точки OI
    params = {
        "category": "linear",
        "symbol": symbol,
        "intervalTime": interval_str,
        "limit": 3
    }
    try:
        res = session.get(url, params=params, timeout=5).json()
        if res.get("retCode") == 0 and res["result"]["list"]:
            records = res["result"]["list"]
            if len(records) >= 2:
                # records[0] — текущее значение, records[1] — предыдущее
                curr_oi = float(records[0]["openInterest"])
                prev_oi = float(records[1]["openInterest"])
                
                if prev_oi > 0:
                    oi_change_pct = ((curr_oi - prev_oi) / prev_oi) * 100
                    return oi_change_pct, curr_oi, prev_oi
    except Exception:
        pass
    return 0.0, 0.0, 0.0

def scan_fakeout_oi(symbol: str):
    candles = get_candles_data(symbol)
    if not candles or len(candles) < LOOKBACK_CANDLES:
        return

    trigger = candles[-1]
    history = candles[:-1]

    range_high = max(c["high"] for c in history)

    # 1. Проверка обновления максимума
    if trigger["high"] <= range_high:
        return

    break_pct = ((trigger["high"] - range_high) / range_high) * 100
    if break_pct < MIN_BREAK_PCT:
        return

    # 2. Фильтр объема (аномальный всплеск)
    history_turnovers = [c["turnover"] for c in history]
    median_vol = statistics.median(history_turnovers)
    mean_vol = statistics.mean(history_turnovers)
    stdev_vol = statistics.stdev(history_turnovers) if len(history_turnovers) > 1 else 1.0

    if median_vol <= 0:
        return

    vol_surge_ratio = trigger["turnover"] / median_vol
    z_score = (trigger["turnover"] - mean_vol) / stdev_vol if stdev_vol > 0 else 0.0

    if vol_surge_ratio < VOLUME_MULTIPLIER or z_score < MIN_Z_SCORE:
        return

    # 3. Фильтр Открытого Интереса: падение OI на пробое
    oi_change_pct, curr_oi, prev_oi = get_oi_change(symbol, trigger["time"])
    
    # Падение OI должно превышать заданный порог (например, <= -1.0%)
    if oi_change_pct > -MIN_OI_DROP_PCT:
        return

    # Расчет верхней тени (признак отторжения цены)
    c_range = trigger["high"] - trigger["low"]
    upper_wick = trigger["high"] - max(trigger["open"], trigger["close"])
    wick_ratio = upper_wick / c_range if c_range > 0 else 0.0

    event_key = (symbol, trigger["time"], "OI_DROP_FAKEOUT")
    if event_key not in notified_events:
        notified_events.add(event_key)
        send_tg(
            f"⚡ <b>ВЫНОС СТОПОВ / ЛОЖНЫЙ ПРОБОЙ (SHORT): {symbol}</b>\n\n"
            f"• Пробиваемый High: <code>{range_high}</code>\n"
            f"• Максимум свечи: <code>{trigger['high']}</code> (Вынос: <code>+{break_pct:.2f}%</code>)\n"
            f"• Закрытие свечи: <code>{trigger['close']}</code>\n"
            f"• Верхняя тень: <code>{wick_ratio * 100:.0f}%</code>\n"
            f"• Объем в USDT: <code>${trigger['turnover']:,.0f}</code>\n"
            f"• Всплеск объема: <code>{vol_surge_ratio:.1f}x</code> (Z: <code>{z_score:.2f}σ</code>)\n"
            f"• <b>Динамика OI:</b> <code>{oi_change_pct:.2f}%</code> (сброс позиций)\n"
            f"• OI: <code>{prev_oi:,.0f}</code> ➔ <code>{curr_oi:,.0f}</code>\n\n"
            f"💡 <i>Механика: Всплеск объема сопровождался ликвидацией/закрытием шортов (падение OI). Новых покупок нет. Возможен Short со стопом за {trigger['high']}.</i>\n"
            f"🔗 <a href='https://www.bybit.com/trade/usdt/{symbol}'>Bybit</a> | "
            f"<a href='https://www.coinglass.com/tv/Bybit_{symbol}'>CoinGlass</a>"
        )

def wait_for_m15_close():
    now = time.time()
    interval = 15 * 60
    sleep_time = interval - (now % interval) + 3
    time.sleep(sleep_time)

def main():
    print("✓ Запуск скринера: Пробой High + Всплеск объема + Падение OI...")
    symbols = get_active_symbols()
    print(f"Отслеживается {len(symbols)} инструментов с оборотом > $15M.")

    while True:
        try:
            wait_for_m15_close()
            print(f"[{time.strftime('%H:%M:%S')}] Свеча M15 закрылась. Проверка сигналов...")

            for s in symbols:
                scan_fakeout_oi(s)
                time.sleep(0.04)

            if len(notified_events) > 300:
                notified_events.clear()

        except Exception as e:
            print(f"Ошибка цикла: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
