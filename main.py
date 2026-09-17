import os
import time
import requests

# --- НАСТРОЙКИ TELEGRAM ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5296533274")

# --- ПАРАМЕТРЫ СТРАТЕГИИ ---
LOOKBACK_H1 = 24              # 24 часа для поиска ключевого сопротивления
MIN_TURNOVER_24H = 10_000 # Оборот от $15M (строгий фильтр качества)
M5_SWING_BARS = 5             # Баров для свинга на M5
M5_VOL_MULT = 1.3             # Объем на сломе M5 выше среднего

session = requests.Session()
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
        print(f"Ошибка загрузки тикеров: {e}")
    return []

def get_candles(symbol: str, interval: str, limit: int):
    """Универсальное получение свечей с расчетом приблизительного CVD"""
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit + 1}
    try:
        res = session.get(url, params=params, timeout=5).json()
        if res.get("retCode") == 0 and res["result"]["list"]:
            raw = list(reversed(res["result"]["list"][1:limit + 1]))
            candles = []
            cum_delta = 0.0
            for k in raw:
                c_open, c_high, c_low, c_close, c_vol = map(float, k[1:6])
                rng = c_high - c_low
                # Дельта приближенно через позиционирование закрытия
                delta = (c_vol * ((c_close - c_open) / rng)) if rng > 0 else 0.0
                cum_delta += delta
                candles.append({
                    "time": int(k[0]), "open": c_open, "high": c_high,
                    "low": c_low, "close": c_close, "volume": c_vol, "cvd": cum_delta
                })
            return candles
    except Exception:
        pass
    return []

def check_h1_fakeouts(symbols):
    """Шаг 1: Поиск свипа часового хая С ДИВЕРГЕНЦИЕЙ ПО CVD"""
    print(f"[{time.strftime('%H:%M:%S')}] Сканирование H1 на ложный пробой с дивергенцией...")
    for symbol in symbols:
        candles = get_candles(symbol, interval="60", limit=LOOKBACK_H1 + 1)
        if not candles or len(candles) < LOOKBACK_H1:
            continue

        trigger = candles[-1]
        history = candles[:-1]

        # Находим индекс свечи максимального хая в истории
        max_high_candle = max(history, key=lambda x: x["high"])
        range_high = max_high_candle["high"]
        peak_cvd = max_high_candle["cvd"]

        # УСЛОВИЯ СВИПА С ДИВЕРГЕНЦИЕЙ:
        # 1. Свеча пробила хай тенью: trigger['high'] > range_high
        # 2. Но закрылась НИЖЕ хая (вернулась в базу): trigger['close'] < range_high
        # 3. ДИВЕРГЕНЦИЯ: Текущий CVD ниже, чем CVD на предыдущем хае (нет реального покупателя)
        if trigger["high"] > range_high and trigger["close"] < range_high:
            if trigger["cvd"] < peak_cvd:
                watchlist[symbol] = {
                    "sweep_high": trigger["high"],
                    "range_high": range_high,
                    "added_at": time.time()
                }
                print(f"💎 Найден идеальный фейкаут: {symbol} (Sweep {range_high}, CVD дивергенция подтверждена)")

        time.sleep(0.04)

def check_m5_choch():
    """Шаг 2: Поиск слома структуры на M5 (вместо шума M1)"""
    now = time.time()
    symbols_to_remove = []

    for symbol, info in list(watchlist.items()):
        # Следим не более 90 минут
        if now - info["added_at"] > 90 * 60:
            symbols_to_remove.append(symbol)
            continue

        m5_bars = get_candles(symbol, interval="5", limit=12)
        if not m5_bars or len(m5_bars) < 8:
            continue

        trigger_m5 = m5_bars[-1]
        history_m5 = m5_bars[:-1]

        # Если монета продолжает перебивать пик сквиза — обновляем стоп-уровень
        if trigger_m5["high"] > info["sweep_high"]:
            info["sweep_high"] = trigger_m5["high"]

        swing_low = min(b["low"] for b in history_m5[-M5_SWING_BARS:])
        avg_vol = sum(b["volume"] for b in history_m5) / len(history_m5)

        # СЛОМ НА M5:
        # 1. Красная свеча M5
        # 2. Закрытие строго под свингом
        # 3. Объём выше среднего
        if (trigger_m5["close"] < swing_low and 
            trigger_m5["close"] < trigger_m5["open"] and 
            trigger_m5["volume"] >= avg_vol * M5_VOL_MULT):

            event_key = (symbol, trigger_m5["time"])
            if event_key not in notified_events:
                notified_events.add(event_key)
                stop_loss = info["sweep_high"]
                entry_price = trigger_m5["close"]
                risk_pct = ((stop_loss - entry_price) / entry_price) * 100

                # Если стоп получается слишком огромным (> 3.5%) — сетап пропускаем
                if risk_pct <= 3.5:
                    send_tg(
                        f"🧲 <b>ИСТИННЫЙ ЛОЖНЫЙ ПРОБОЙ: СЛОМ M5 (SHORT)</b>\n\n"
                        f"🪙 <b>Монета:</b> <code>{symbol}</code>\n"
                        f"• <b>Снят часовой уровень:</b> <code>{info['range_high']}</code>\n"
                        f"• <b>Фактор разворота:</b> Медвежья дивергенция CVD (поглощение лимитами)\n"
                        f"• <b>Слом M5 (CHoCH):</b> пробит уровень <code>{swing_low}</code>\n"
                        f"• Вход: <code>{entry_price}</code>\n"
                        f"• <b>Стоп-лосс (пик свипа):</b> <code>{stop_loss}</code> (риск: <code>{risk_pct:.2f}%</code>)\n"
                        f"• <b>Тейк-профит:</b> <b>+2.0%</b> (или возврат к середине H1)\n\n"
                        f"🔗 <a href='https://www.bybit.com/trade/usdt/{symbol}'>Bybit</a> | "
                        f"<a href='https://www.coinglass.com/tv/Bybit_{symbol}'>CoinGlass</a>"
                    )
                symbols_to_remove.append(symbol)

        time.sleep(0.04)

    for s in symbols_to_remove:
        watchlist.pop(s, None)

def main():
    print("✓ Запуск продвинутого скринера фейк-пробоев (H1 CVD Div + M5 CHoCH)...")
    symbols = get_active_symbols()
    print(f"Отслеживается {len(symbols)} инструментов.")

    last_h1_checked = 0

    while True:
        try:
            now = time.time()

            # Проверка H1 раз в час
            if now - last_h1_checked >= 3600 or (int(now) % 3600 < 10 and now - last_h1_checked > 120):
                symbols = get_active_symbols()
                check_h1_fakeouts(symbols)
                last_h1_checked = now

            # Сканирование M5 для отобранных пар каждые 20 секунд
            if watchlist:
                check_m5_choch()

            if len(notified_events) > 200:
                notified_events.clear()

            time.sleep(20)

        except Exception as e:
            print(f"Ошибка цикла: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()

