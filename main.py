import os
import time
import statistics
import requests

# --- Настройки Telegram ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5296533274")

# --- Параметры стратегии: Свип 15m + Сброс OI + ChoCh 1m ---
TIMEFRAME = "15"               # Старший таймфрейм
LOOKBACK_CANDLES = 48          # 12 часов истории
MIN_BREAK_PCT = 2            # Закол хая минимум на +0.4%
VOLUME_MULTIPLIER = 2.0        # Объем M15 >= 2x от медианы
MIN_Z_SCORE = 1.8              # Z-Score объема M15
MIN_OI_DROP_PCT = 0.8          # Падение OI на M15 минимум на -0.8%
MIN_TURNOVER_24H = 1_000_000  # Ликвидность: от $15 млн (защита от неликвида)

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

def get_candles(symbol: str, interval: str, limit: int):
    """Универсальная загрузка свечей."""
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
    try:
        res = session.get(url, params=params, timeout=5).json()
        if res.get("retCode") == 0 and res["result"]["list"]:
            raw = list(reversed(res["result"]["list"]))
            return [{
                "time": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "turnover": float(k[6])
            } for k in raw]
    except Exception:
        pass
    return []

def get_oi_change(symbol: str):
    url = "https://api.bybit.com/v5/market/open-interest"
    params = {"category": "linear", "symbol": symbol, "intervalTime": "15min", "limit": 3}
    try:
        res = session.get(url, params=params, timeout=5).json()
        if res.get("retCode") == 0 and res["result"]["list"]:
            records = res["result"]["list"]
            if len(records) >= 2:
                curr_oi = float(records[0]["openInterest"])
                prev_oi = float(records[1]["openInterest"])
                if prev_oi > 0:
                    return ((curr_oi - prev_oi) / prev_oi) * 100, curr_oi, prev_oi
    except Exception:
        pass
    return 0.0, 0.0, 0.0

def verify_m1_choch(symbol: str):
    """
    Проверяет слом структуры на M1 (ChoCh):
    1. Ищет локальный Higher Low перед абсолютным пиком на M1.
    2. Проверяет закрытие свечи M1 телом ниже этого HL.
    3. Проверяет объем свечи слома (должен превышать средний M1).
    """
    m1_candles = get_candles(symbol, interval="1", limit=16)
    if not m1_candles or len(m1_candles) < 15:
        return False, 0.0, 0.0

    # Анализируем последние 15 закрытых минутных свечей
    window = m1_candles[:-1]
    
    # Индекс наивысшей точки
    peak_idx = max(range(len(window)), key=lambda i: window[i]["high"])
    
    # Если пик на самой первой или самой последней свече — сформированной структуры нет
    if peak_idx < 2 or peak_idx >= len(window) - 1:
        return False, 0.0, 0.0

    # Ищем Higher Low (минимум, с которого пошел импульс на пик)
    hl_candidates = [c["low"] for c in window[:peak_idx]]
    if not hl_candidates:
        return False, 0.0, 0.0
    hl_level = min(hl_candidates[-3:]) # ближайший локальный лой перед хаем

    # Проверяем свечи ПОСЛЕ пика: был ли пробой лоя телом на объеме
    avg_m1_vol = statistics.mean([c["turnover"] for c in window])
    for c in window[peak_idx + 1:]:
        if c["close"] < hl_level and c["turnover"] > avg_m1_vol * 1.3:
            return True, hl_level, window[peak_idx]["high"]

    return False, 0.0, 0.0

def scan_full_short_setup(symbol: str):
    candles = get_candles(symbol, interval=TIMEFRAME, limit=LOOKBACK_CANDLES + 2)
    if not candles or len(candles) < LOOKBACK_CANDLES:
        return

    # Берем последнюю закрытую 15m свечу
    trigger = candles[-2]
    history = candles[:-2]

    range_high = max(c["high"] for c in history)

    # 1. Закол хая (High вышел выше, но закрытие СТРОГО ниже уровня — Sweep)
    if trigger["high"] <= range_high or trigger["close"] >= range_high:
        return

    break_pct = ((trigger["high"] - range_high) / range_high) * 100
    if break_pct < MIN_BREAK_PCT:
        return

    # 2. Объем M15 (Всплеск за счет выноса стопов)
    turnovers = [c["turnover"] for c in history]
    median_vol = statistics.median(turnovers)
    mean_vol = statistics.mean(turnovers)
    stdev_vol = statistics.stdev(turnovers) if len(turnovers) > 1 else 1.0

    vol_surge = trigger["turnover"] / median_vol if median_vol > 0 else 0
    z_score = (trigger["turnover"] - mean_vol) / stdev_vol if stdev_vol > 0 else 0

    if vol_surge < VOLUME_MULTIPLIER or z_score < MIN_Z_SCORE:
        return

    # 3. Открытый Интерес (OI упал = топлива для лонга больше нет)
    oi_drop, curr_oi, prev_oi = get_oi_change(symbol)
    if oi_drop > -MIN_OI_DROP_PCT:
        return

    # 4. Подтверждение разворота: Проверка ChoCh на M1
    has_choch, broken_hl, peak_high = verify_m1_choch(symbol)
    if not has_choch:
        return

    event_key = (symbol, trigger["time"], "SNIPER_SHORT_CHOCH")
    if event_key not in notified_events:
        notified_events.add(event_key)
        send_tg(
            f"🎯 <b>ИДЕАЛЬНЫЙ ШОРТ-СЕТАП (SWEEP + DROP OI + M1 CHOCH): {symbol}</b>\n\n"
            f"• Пробиваемый High: <code>{range_high}</code>\n"
            f"• Свип тенью: <code>{trigger['high']}</code> (<code>+{break_pct:.2f}%</code>)\n"
            f"• Возврат под уровень: Close <code>{trigger['close']}</code> &lt; High\n"
            f"• Объем M15: <code>{vol_surge:.1f}x</code> к норме (Z: <code>{z_score:.2f}σ</code>)\n"
            f"• <b>Сброс OI:</b> <code>{oi_drop:.2f}%</code> (выбиты стопы)\n"
            f"• <b>M1 ChoCh:</b> сломан лой <code>{broken_hl}</code> телом на объеме\n\n"
            f"📍 <i>План: Вход в шорт на ретесте сломанного уровня {broken_hl} или M1 FVG. Стоп за пик: {peak_high}.</i>\n"
            f"🔗 <a href='https://www.bybit.com/trade/usdt/{symbol}'>Bybit</a> | "
            f"<a href='https://www.coinglass.com/tv/Bybit_{symbol}'>CoinGlass</a>"
        )

def wait_for_m15_close():
    now = time.time()
    interval = 15 * 60
    sleep_time = interval - (now % interval) + 3
    time.sleep(sleep_time)

def main():
    print("✓ Запуск снайпер-скринера (M15 Sweep + OI Drop + M1 ChoCh)...")
    symbols = get_active_symbols()
    print(f"Отслеживается {len(symbols)} ликвидных пар.")

    while True:
        try:
            wait_for_m15_close()
            print(f"[{time.strftime('%H:%M:%S')}] Свеча M15 закрылась. Фильтрация...")

            for s in symbols:
                scan_full_short_setup(s)
                time.sleep(0.04)

            if len(notified_events) > 300:
                notified_events.clear()

        except Exception as e:
            print(f"Ошибка цикла: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
