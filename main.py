import os
import time
import requests

# --- НАСТРОЙКИ TELEGRAM ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5296533274")

# --- ПАРАМЕТРЫ СТАРШЕГО ТАЙМФРЕЙМА (H1) ---
TIMEFRAME_H1 = "60"           # Часовые свечи на Bybit V5
LOOKBACK_H1 = 12              # 24 свечи (суточный экстремум / 24 часа истории)
MIN_BREAK_PCT_H1 = 0.8        # Минимальный вынос хая (от 0.8%)
MAX_BREAK_PCT_H1 = 5.0        # Максимальный вынос хая (если выше — бешеный туземун, не лезем)
VOL_MULT_H1 = 2.0             # Всплеск объема на H1 (в 2 раза выше среднего)
MIN_TURNOVER_24H = 10_000 # Фильтр ликвидности (суточный оборот от $10M)

# --- ПАРАМЕТРЫ МЛАДШЕГО ТАЙМФРЕЙМА (M1) ---
M1_WATCH_EXPIRE_SEC = 120 * 60  # Следить за монетой на M1 в течение 120 минут после закрытия H1
M1_VOLUME_MULT = 1.5            # Объем на минутной свече слома (в 1.5 раза выше среднего M1)
SWING_LOW_BARS_M1 = 5           # Количество баров для поиска локального свингового минимума

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
            # Исключаем индекс 0 (текущая незакрытая свеча), берем закрытые бары
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

def check_h1_candidates(symbols):
    """Проверяет пробой суточного хая на H1 и ставит монету на карантин в Watchlist"""
    print(f"[{time.strftime('%H:%M:%S')}] Свеча H1 закрылась. Сканирование выносов суточного хая...")
    for symbol in symbols:
        candles = get_h1_data(symbol)
        if not candles or len(candles) < LOOKBACK_H1:
            continue

        trigger = candles[-1]
        history = candles[:-1]

        range_high = max(c["high"] for c in history)
        max_history_cvd = max(c["cvd"] for c in history)
        avg_vol = sum(c["volume"] for c in history) / len(history)

        c_range = trigger["high"] - trigger["low"]
        if c_range == 0:
            continue

        # Условия кульминации покупок на часовике:
        # 1. Свеча закрылась выше суточного хая
        # 2. Вынос тела от 0.8% до 5.0%
        # 3. Закрытие полнотелое зеленое
        # 4. Верхняя тень маленькая (<= 25%) — толпа давила до конца часа
        # 5. Объем H1 в 2x выше среднего
        # 6. Рекордный всплеск CVD
        if trigger["close"] > range_high:
            break_pct = ((trigger["close"] - range_high) / range_high) * 100
            upper_wick = trigger["high"] - max(trigger["open"], trigger["close"])
            wick_ratio = upper_wick / c_range

            if (MIN_BREAK_PCT_H1 <= break_pct <= MAX_BREAK_PCT_H1 and
                trigger["close"] > trigger["open"] and
                wick_ratio <= 0.25 and
                trigger["volume"] >= avg_vol * VOL_MULT_H1 and
                trigger["cvd"] > max_history_cvd):

                watchlist[symbol] = {
                    "pump_high": trigger["high"],
                    "break_level": range_high,
                    "added_at": time.time()
                }
                print(f"👀 {symbol} добавлен в Watchlist! (Суточный High: {range_high}, Пик: {trigger['high']})")
        
        time.sleep(0.04)

def check_m1_choch():
    """Мониторит кандидатов из Watchlist на M1 в поисках слома структуры (CHoCH) на объеме"""
    now = time.time()
    symbols_to_remove = []

    for symbol, info in list(watchlist.items()):
        # Если за 2 часа слом так и не произошел — удаляем монету
        if now - info["added_at"] > M1_WATCH_EXPIRE_SEC:
            symbols_to_remove.append(symbol)
            continue

        m1_bars = get_m1_candles(symbol, limit=15)
        if not m1_bars or len(m1_bars) < 10:
            continue

        trigger_m1 = m1_bars[-1]
        history_m1 = m1_bars[:-1]

        # Подтягиваем пик пампа, если цена продолжает обновлять High
        if trigger_m1["high"] > info["pump_high"]:
            info["pump_high"] = trigger_m1["high"]

        # Находим локальный свинговый минимум последних N минутных свечей
        swing_low = min(b["low"] for b in history_m1[-SWING_LOW_BARS_M1:])
        avg_m1_vol = sum(b["volume"] for b in history_m1) / len(history_m1)

        # КРИТЕРИИ СЛОМА СТРУКТУРЫ (CHoCH) НА M1:
        # 1. Свеча закрылась КРАСНОЙ (Close < Open)
        # 2. Свеча телом пробила свинговый минимум (Close < swing_low)
        # 3. Всплеск объема на минутном баре слома в 1.5+ раза
        if (trigger_m1["close"] < swing_low and 
            trigger_m1["close"] < trigger_m1["open"] and 
            trigger_m1["volume"] >= avg_m1_vol * M1_VOLUME_MULT):

            event_key = (symbol, trigger_m1["time"], "H1_BREAK_M1_CHOCH")
            if event_key not in notified_events:
                notified_events.add(event_key)
                stop_loss = info["pump_high"]
                curr_price = trigger_m1["close"]
                stop_distance_pct = ((stop_loss - curr_price) / curr_price) * 100

                send_tg(
                    f"🎯 <b>РАЗГРУЗКА ПОСЛЕ ВЫНОСА H1: СЛОМ СТРУКТУРЫ (SHORT)</b>\n\n"
                    f"🪙 <b>Монета:</b> <code>{symbol}</code>\n"
                    f"• Пробит суточный уровень High (H1): <code>{info['break_level']}</code>\n"
                    f"• Пробит свинговый Low (M1): <code>{swing_low}</code>\n"
                    f"• Текущая цена (Вход): <code>{curr_price}</code>\n"
                    f"• <b>Объём на сломе M1:</b> <code>{trigger_m1['volume']/avg_m1_vol:.1f}x</code> от среднего\n"
                    f"• <b>Пик пампа (Стоп-лосс):</b> <code>{stop_loss}</code> (риск: <code>{stop_distance_pct:.2f}%</code>)\n"
                    f"• <b>Тейк-профит:</b> фиксированные <b>+2.0%</b> от входа\n\n"
                    f"💡 <i>Толпу заперли в лонгах на часовике. На M1 маркетмейкер отдал инициативу продавцам.</i>\n"
                    f"🔗 <a href='https://www.bybit.com/trade/usdt/{symbol}'>Bybit</a> | "
                    f"<a href='https://www.coinglass.com/tv/Bybit_{symbol}'>CoinGlass</a>"
                )
                symbols_to_remove.append(symbol)

        time.sleep(0.05)

    for s in symbols_to_remove:
        watchlist.pop(s, None)

def main():
    print("✓ Запуск скринера (H1 Breakout + M1 CHoCH on Volume)...")
    symbols = get_active_symbols()
    print(f"Отслеживается {len(symbols)} инструментов с оборотом > ${MIN_TURNOVER_24H/1e6:.0f}M.")

    last_h1_checked = 0

    while True:
        try:
            now = time.time()

            # Проверка H1 раз в 1 час (в первые секунды нового часа)
            if now - last_h1_checked >= 3600 or (int(now) % 3600 < 10 and now - last_h1_checked > 120):
                symbols = get_active_symbols()
                check_h1_candidates(symbols)
                last_h1_checked = now

            # Сканирование M1 для монет из Watchlist (каждые 15 секунд)
            if watchlist:
                check_m1_choch()

            # Очистка старых событий, чтобы не забивать память
            if len(notified_events) > 200:
                notified_events.clear()

            time.sleep(15)

        except Exception as e:
            print(f"Ошибка главного цикла: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
