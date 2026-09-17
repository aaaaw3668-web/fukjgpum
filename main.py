import os
import time
import requests

# --- НАСТРОЙКИ TELEGRAM ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5296533274")

# --- ПАРАМЕТРЫ СТАРШЕГО ТАЙМФРЕЙМА (H1: ЛОЖНЫЙ ПРОБОЙ СОПРОТИВЛЕНИЯ) ---
TIMEFRAME_H1 = "60"           # Часовые свечи Bybit V5
LOOKBACK_H1 = 24              # 24 свечи (суточная база / 24 часа истории)
MIN_SWEEP_PCT_H1 = 0.4        # Минимальный прокол сопротивления (от 0.4%)
MAX_SWEEP_PCT_H1 = 4.0        # Максимальный прокол (если выше — бешеный памп, не трогаем)
VOL_MULT_H1 = 1.8             # Всплеск объема на H1 (в 1.8x выше среднего)
MIN_TURNOVER_24H = 10_000_000 # Оборот от $10M (защита от неликвида)

# --- ПАРАМЕТРЫ МЛАДШЕГО ТАЙМФРЕЙМА (M1: МЕДВЕЖИЙ СЛОМ CHoCH) ---
M1_WATCH_EXPIRE_SEC = 120 * 60  # Время наблюдения (до 2 часов после закрытия H1)
M1_VOLUME_MULT = 1.4            # Всплеск объема на свече слома M1
SWING_LOW_BARS_M1 = 5           # Количество минутных баров для поиска локального свингового Low

session = requests.Session()
# Список наблюдения: { symbol: {"sweep_high": float, "resistance_level": float, "added_at": float} }
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
        print(f"Ошибка получения списка тикеров: {e}")
    return []

def get_h1_data(symbol: str):
    """Сбор часовых свечей с расчетом дельты и кумулятивного CVD"""
    url = "https://api.bybit.com/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": TIMEFRAME_H1,
        "limit": LOOKBACK_H1 + 2
    }
    try:
        res = session.get(url, params=params, timeout=5).json()
        if res.get("retCode") == 0 and res["result"]["list"]:
            raw_candles = list(reversed(res["result"]["list"][1:LOOKBACK_H1 + 1]))
            candles = []
            cum_delta = 0.0

            for k in raw_candles:
                c_open, c_high, c_low, c_close, c_vol = map(float, k[1:6])
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
                    "cvd": cum_delta
                })
            return candles
    except Exception:
        pass
    return []

def get_m1_candles(symbol: str, limit: int = 20):
    """Сбор закрытых минутных свечей"""
    url = "https://api.bybit.com/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": "1",
        "limit": limit + 1
    }
    try:
        res = session.get(url, params=params, timeout=5).json()
        if res.get("retCode") == 0 and res["result"]["list"]:
            raw = list(reversed(res["result"]["list"][1:limit + 1]))
            return [{
                "time": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5])
            } for k in raw]
    except Exception:
        pass
    return []

def check_h1_fake_breakout(symbols):
    """Шаг 1: Детект ложного выноса часового сопротивления (H1 Resistance Sweep)"""
    print(f"[{time.strftime('%H:%M:%S')}] Свеча H1 закрыта. Поиск ложных выносов сопротивления...")
    for symbol in symbols:
        candles = get_h1_data(symbol)
        if not candles or len(candles) < LOOKBACK_H1:
            continue

        trigger = candles[-1]
        history = candles[:-1]

        # Находим ключевую линию сопротивления за Lookback
        range_high = max(c["high"] for c in history)
        avg_vol = sum(c["volume"] for c in history) / len(history)

        c_range = trigger["high"] - trigger["low"]
        if c_range == 0:
            continue

        # Прокол сопротивления хаем свечи
        if trigger["high"] > range_high:
            sweep_pct = ((trigger["high"] - range_high) / range_high) * 100
            upper_wick = trigger["high"] - max(trigger["open"], trigger["close"])
            wick_ratio = upper_wick / c_range  # Доля верхнего фитиля от размаха бара

            # УСЛОВИЯ ЛОЖНОГО ПРОБОЯ (FAKEOUT RESISTANCE):
            # 1. Глубина выноса от 0.4% до 4.0%
            # 2. Цена закрылась ОБРАТНО ПОД сопротивлением ЛИБО оставила длинную тень отката (>= 45%)
            # 3. Аномальный объем (кульминация покупок толпы)
            is_closed_below = trigger["close"] <= range_high
            is_strong_wick = wick_ratio >= 0.45

            if (MIN_SWEEP_PCT_H1 <= sweep_pct <= MAX_SWEEP_PCT_H1 and
                (is_closed_below or is_strong_wick) and
                trigger["volume"] >= avg_vol * VOL_MULT_H1):

                watchlist[symbol] = {
                    "sweep_high": trigger["high"],
                    "resistance_level": range_high,
                    "added_at": time.time()
                }
                print(f"🧲 Ложный вынос H1 на {symbol}! Сопротивление: {range_high}, Пик сквиза: {trigger['high']}")

        time.sleep(0.04)

def check_m1_bearish_choch():
    """Шаг 2: Поиск медвежьего слома структуры (CHoCH вниз) на M1"""
    now = time.time()
    symbols_to_remove = []

    for symbol, info in list(watchlist.items()):
        # Истек таймаут наблюдения
        if now - info["added_at"] > M1_WATCH_EXPIRE_SEC:
            symbols_to_remove.append(symbol)
            continue

        m1_bars = get_m1_candles(symbol, limit=15)
        if not m1_bars or len(m1_bars) < 10:
            continue

        trigger_m1 = m1_bars[-1]
        history_m1 = m1_bars[:-1]

        # Если монета продолжает импульсно обновлять пик — подтягиваем стоп
        if trigger_m1["high"] > info["sweep_high"]:
            info["sweep_high"] = trigger_m1["high"]

        # Ищем локальный свинговый минимум последних N минутных свечей
        swing_low = min(b["low"] for b in history_m1[-SWING_LOW_BARS_M1:])
        avg_m1_vol = sum(b["volume"] for b in history_m1) / len(history_m1)

        # КРИТЕРИИ МЕДВЕЖЬЕГО СЛОМА (BEARISH CHoCH):
        # 1. Минутная свеча закрылась КРАСНОЙ (Close < Open)
        # 2. Закрытие строго НИЖЕ свингового минимума (Close < swing_low)
        # 3. Объем свечи слома в 1.4x выше среднего M1
        if (trigger_m1["close"] < swing_low and
            trigger_m1["close"] < trigger_m1["open"] and
            trigger_m1["volume"] >= avg_m1_vol * M1_VOLUME_MULT):

            event_key = (symbol, trigger_m1["time"], "H1_FAKEOUT_M1_BEAR_CHOCH")
            if event_key not in notified_events:
                notified_events.add(event_key)
                stop_loss = info["sweep_high"]
                curr_price = trigger_m1["close"]
                stop_distance_pct = ((stop_loss - curr_price) / curr_price) * 100

                send_tg(
                    f"🔴 <b>ЛОЖНЫЙ ПРОБОЙ СОПРОТИВЛЕНИЯ H1: СЛОМ В ШОРТ (CHoCH)</b>\n\n"
                    f"🪙 <b>Монета:</b> <code>{symbol}</code>\n"
                    f"• Протестировано сопротивление (H1): <code>{info['resistance_level']}</code>\n"
                    f"• Пробит свинговый Low (M1): <code>{swing_low}</code>\n"
                    f"• Текущая цена (Вход SHORT): <code>{curr_price}</code>\n"
                    f"• <b>Объём слома M1:</b> <code>{trigger_m1['volume']/avg_m1_vol:.1f}x</code>\n"
                    f"• <b>Пик манипуляции (Стоп-лосс):</b> <code>{stop_loss}</code> (риск: <code>{stop_distance_pct:.2f}%</code>)\n"
                    f"• <b>Тейк-профит:</b> фиксированные <b>+2.0%</b>\n\n"
                    f"💡 <i>Маркетмейкер собрал стоп-ликвидность шортистов над уровнем сопротивления H1. На минутке продавцы подтвердили перехват инициативы.</i>\n"
                    f"🔗 <a href='https://www.bybit.com/trade/usdt/{symbol}'>Bybit</a> | "
                    f"<a href='https://www.coinglass.com/tv/Bybit_{symbol}'>CoinGlass</a>"
                )
                symbols_to_remove.append(symbol)

        time.sleep(0.05)

    for s in symbols_to_remove:
        watchlist.pop(s, None)

def main():
    print("✓ Запуск скринера (H1 Fakeout Resistance + M1 Bearish CHoCH)...")
    symbols = get_active_symbols()
    print(f"Отслеживается {len(symbols)} инструментов с оборотом > ${MIN_TURNOVER_24H/1e6:.0f}M.")

    last_h1_checked = 0

    while True:
        try:
            now = time.time()

            # Проверка H1 раз в час при закрытии бара (:00 минут)
            if now - last_h1_checked >= 3600 or (int(now) % 3600 < 10 and now - last_h1_checked > 120):
                symbols = get_active_symbols()
                check_h1_fake_breakout(symbols)
                last_h1_checked = now

            # Проверка M1 для кандидатов из списка наблюдения
            if watchlist:
                check_m1_bearish_choch()

            if len(notified_events) > 200:
                notified_events.clear()

            time.sleep(15)

        except Exception as e:
            print(f"Ошибка главного цикла: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
