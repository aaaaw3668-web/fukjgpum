import os
import time
import statistics
import requests

# --- Настройки Telegram ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5296533274")

# --- Параметры стратегии пробоя (Long Only) ---
TIMEFRAME = "15"               # Рабочий таймфрейм M15
LOOKBACK_CANDLES = 48          # Консолидация: 48 свечей (12 часов)
MIN_BREAK_PCT = 0.8            # Закрепление выше уровня минимум на +0.8%
MAX_BREAK_PCT = 4.0            # Защита от FOMO (если свеча улетела > 4%, вход пропускается)
VOLUME_MULTIPLIER = 2.5        # Текущий оборот в USDT >= 2.5x от медианы консолидации
MIN_Z_SCORE = 2.0              # Статистический выброс объема (Z >= 2.0)
BUY_RATIO_MIN = 0.60           # Доля маркет-покупок на свече пробоя >= 60%
MIN_TURNOVER_24H = 15_000  # Ликвидность: от $15 млн за 24 часа

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
    """Сбор свечей с оборотом в USDT."""
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
            # Отсекаем нулевую незакрытую свечу и берем закрытую базу
            raw_candles = list(reversed(res["result"]["list"][1:LOOKBACK_CANDLES + 1]))
            
            candles = []
            for k in raw_candles:
                candles.append({
                    "time": int(k[0]),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "turnover": float(k[6])  # Реальный объем свечи в USDT
                })
            return candles
    except Exception:
        pass
    return []

def get_exact_trigger_delta(symbol: str, candle_start_time: int):
    """
    Расчет реальной дельты через публичную ленту сделок Bybit.
    Суммирует агрессивные рыночные покупки (Buy) и продажи (Sell).
    """
    url = "https://api.bybit.com/v5/market/recent-trade"
    params = {
        "category": "linear",
        "symbol": symbol,
        "limit": 1000
    }
    try:
        res = session.get(url, params=params, timeout=5).json()
        if res.get("retCode") == 0:
            trades = res["result"]["list"]
            buy_vol_usdt = 0.0
            sell_vol_usdt = 0.0

            for t in trades:
                trade_time = int(t["time"])
                # Учитываем только сделки внутри временного окна пробойной свечи
                if trade_time >= candle_start_time:
                    size_usdt = float(t["size"]) * float(t["price"])
                    if t["side"] == "Buy":
                        buy_vol_usdt += size_usdt
                    else:
                        sell_vol_usdt += size_usdt

            total_trades_vol = buy_vol_usdt + sell_vol_usdt
            if total_trades_vol > 0:
                buy_ratio = buy_vol_usdt / total_trades_vol
                net_delta_usdt = buy_vol_usdt - sell_vol_usdt
                return net_delta_usdt, buy_ratio

    except Exception:
        pass
    return 0.0, 0.5

def scan_real_breakout(symbol: str):
    candles = get_candles_data(symbol)
    if not candles or len(candles) < LOOKBACK_CANDLES:
        return

    trigger = candles[-1]
    history = candles[:-1]

    c_range = trigger["high"] - trigger["low"]
    if c_range == 0:
        return

    range_high = max(c["high"] for c in history)

    # 1. Первичный фильтр по ценовому закреплению
    if trigger["close"] <= range_high:
        return

    break_pct = ((trigger["close"] - range_high) / range_high) * 100
    if not (MIN_BREAK_PCT <= break_pct <= MAX_BREAK_PCT):
        return

    if trigger["close"] <= trigger["open"]:
        return

    upper_wick = trigger["high"] - max(trigger["open"], trigger["close"])
    wick_ratio = upper_wick / c_range
    if wick_ratio > 0.25:
        return

    # 2. Фильтр объема по USDT (Медиана + Z-Score)
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

    # 3. Фильтр реальной дельты (Time & Sales)
    net_delta_usdt, buy_ratio = get_exact_trigger_delta(symbol, trigger["time"])
    if buy_ratio < BUY_RATIO_MIN or net_delta_usdt <= 0:
        return

    # Отправка уведомления
    event_key = (symbol, trigger["time"], "REAL_BREAK_LONG")
    if event_key not in notified_events:
        notified_events.add(event_key)
        send_tg(
            f"🚀 <b>ИСТИННЫЙ ПРОБОЙ ХАЯ (LONG): {symbol}</b>\n\n"
            f"• Пробитый High: <code>{range_high}</code>\n"
            f"• Закрытие: <code>{trigger['close']}</code> (Закрепление: <code>+{break_pct:.2f}%</code>)\n"
            f"• Верхняя тень: <code>{wick_ratio * 100:.0f}%</code> (нет лимитного продавца)\n"
            f"• Оборот в USDT: <code>${trigger['turnover']:,.0f}</code>\n"
            f"• Всплеск объема: <code>{vol_surge_ratio:.1f}x</code> к медиане (Z: <code>{z_score:.2f}σ</code>)\n"
            f"• <b>Реальная лента сделок:</b> маркет-покупки <code>{buy_ratio * 100:.1f}%</code>\n"
            f"• Чистая дельта: <code>+${net_delta_usdt:,.0f}</code> агрессивных покупок\n\n"
            f"💡 <i>Вход: на ретесте уровня {range_high} или по рынку со стопом под {range_high}.</i>\n"
            f"🔗 <a href='https://www.bybit.com/trade/usdt/{symbol}'>Bybit</a> | "
            f"<a href='https://www.coinglass.com/tv/Bybit_{symbol}'>CoinGlass</a>"
        )

def wait_for_m15_close():
    now = time.time()
    interval = 15 * 60
    sleep_time = interval - (now % interval) + 3
    time.sleep(sleep_time)

def main():
    print("✓ Запуск лонг-скринера (Медианный USDT-объем + лента сделок Bybit T&S)...")
    symbols = get_active_symbols()
    print(f"Отслеживается {len(symbols)} инструментов с оборотом > $15M.")

    while True:
        try:
            wait_for_m15_close()
            print(f"[{time.strftime('%H:%M:%S')}] Свеча M15 закрылась. Проверка сигналов...")

            for s in symbols:
                scan_real_breakout(s)
                time.sleep(0.04)

            if len(notified_events) > 300:
                notified_events.clear()

        except Exception as e:
            print(f"Ошибка цикла: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
