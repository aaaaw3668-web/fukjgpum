import os
import time
import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5296533274")

# Параметры скринера M15
LOOKBACK_M15 = 48             # Окно анализа: 48 свечей (12 часов)
MIN_BREAK_PCT_M15 = 0.5       # Минимальный пробой уровня (%)
MAX_BREAK_PCT_M15 = 4.0       # Максимальный пробой (%)
VOL_MULT_M15 = 1.5            # Множитель среднего объема
MIN_TURNOVER_24H = 10_000 # Минимальный суточный объем ($10M)

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
        print(f"Ошибка тикеров: {e}")
    return []

def get_m15_data(symbol: str):
    """Получение свечей M15 с расчетом синтетического CVD"""
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "linear", "symbol": symbol, "interval": "15", "limit": LOOKBACK_M15 + 2}
    try:
        res = session.get(url, params=params, timeout=5).json()
        if res.get("retCode") == 0 and res["result"]["list"]:
            # Исключаем текущую незакрытую свечу (индекс 0) и берем историю
            raw_candles = list(reversed(res["result"]["list"][1:LOOKBACK_M15 + 1]))
            candles = []
            cum_delta = 0.0
            
            for k in raw_candles:
                c_open, c_high, c_low, c_close, c_vol = map(float, k[1:6])
                c_range = c_high - c_low
                # Синтетическая дельта свечи на основе позиционирования закрытия
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

def scan_cvd_divergence(symbols):
    """Поиск пробоя уровня с медвежьей дивергенцией CVD"""
    print(f"[{time.strftime('%H:%M:%S')}] Сканирование M15 на дивергенцию CVD...")

    for symbol in symbols:
        candles = get_m15_data(symbol)
        if not candles or len(candles) < LOOKBACK_M15:
            continue

        trigger = candles[-1]
        history = candles[:-1]

        range_high = max(c["high"] for c in history)
        max_history_cvd = max(c["cvd"] for c in history)
        avg_vol = sum(c["volume"] for c in history) / len(history)

        c_range = trigger["high"] - trigger["low"]
        if c_range == 0:
            continue

        # УСЛОВИЕ 1: Пробой ценового High диапазона
        price_breakout = trigger["close"] > range_high
        
        # УСЛОВИЕ 2: Медвежья дивергенция по CVD (цена на пике, а CVD ниже пика диапазона)
        cvd_divergence = trigger["cvd"] < max_history_cvd

        if price_breakout and cvd_divergence:
            break_pct = ((trigger["close"] - range_high) / range_high) * 100

            if (MIN_BREAK_PCT_M15 <= break_pct <= MAX_BREAK_PCT_M15 and
                trigger["volume"] >= avg_vol * VOL_MULT_M15):

                event_key = (symbol, trigger["time"], "CVD_DIV")
                if event_key not in notified_events:
                    notified_events.add(event_key)
                    
                    stop_loss = trigger["high"]
                    curr_price = trigger["close"]
                    risk_pct = ((stop_loss - curr_price) / curr_price) * 100

                    send_tg(
                        f"⚠️ <b>ПРОБОЙ С МЕДВЕЖЬЕЙ ДИВЕРГЕНЦИЕЙ CVD: {symbol}</b>\n\n"
                        f"• Уровень пробоя: <code>{range_high}</code>\n"
                        f"• Цена закрытия: <code>{curr_price}</code> (+{break_pct:.2f}%)\n"
                        f"• Объем: <code>{trigger['volume'] / avg_vol:.1f}x</code> от среднего\n"
                        f"• Текущий CVD: <code>{trigger['cvd']:,.0f}</code>\n"
                        f"• Макс. CVD базы: <code>{max_history_cvd:,.0f}</code>\n"
                        f"• Стоп-лосс (High): <code>{stop_loss}</code> (риск: <code>{risk_pct:.2f}%</code>)\n\n"
                        f"📉 <i>Цена показала пробой максимума, но аккумуляция дельты (CVD) падает. Маркет-покупатель отсутствует, рост происходит на пассивном лимитном исполнении.</i>\n"
                        f"🔗 <a href='https://www.bybit.com/trade/usdt/{symbol}'>Bybit</a> | "
                        f"<a href='https://www.coinglass.com/tv/Bybit_{symbol}'>CoinGlass</a>"
                    )
                    print(f"🎯 Найдена дивергенция на {symbol}")
        time.sleep(0.04)

def main():
    print("✓ Запуск скринера дивергенций CVD на M15...")
    symbols = get_active_symbols()
    print(f"Отслеживается {len(symbols)} инструментов.")

    last_m15_checked = 0

    while True:
        try:
            now = time.time()
            # Проверка каждые 15 минут в момент закрытия свечи
            if now - last_m15_checked >= 900 or (int(now) % 900 < 5 and now - last_m15_checked > 60):
                symbols = get_active_symbols()
                scan_cvd_divergence(symbols)
                last_m15_checked = now

            if len(notified_events) > 200:
                notified_events.clear()

            time.sleep(10)

        except Exception as e:
            print(f"Ошибка главного цикла: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
