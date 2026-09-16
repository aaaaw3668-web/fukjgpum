import asyncio
import json
import os
import time
import requests
import websockets

# --- НАСТРОЙКИ TELEGRAM ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5296533274")

# --- ПАРАМЕТРЫ СТРАТЕГИИ ---
MIN_TURNOVER_24H = 10_000       # Фильтр ликвидности ($10M)
LIQ_WINDOW_SEC = 120                # Окно накопления ликвидаций (2 минуты)
MIN_LIQ_VOLUME_USD = 5_000         # Сумма ликвидаций шортов для триггера ($80k+)
M1_VOLUME_MULT = 1.4                # Объем свечи M1 со сломом (1.4x выше среднего)
WATCH_EXPIRE_SEC = 30 * 60          # Следить за сломом не более 30 минут

BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"

# Хранилища состояний в памяти:
# liquidations_log: { symbol: [ (timestamp, volume_usd), ... ] }
liquidations_log = {}
# watchlist: { symbol: { "pump_high": float, "liq_total": float, "added_at": float } }
watchlist = {}
# m1_history: { symbol: [ {"close": float, "open": float, "high": float, "low": float, "vol": float}, ... ] }
m1_history = {}
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
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Ошибка отправки TG: {e}")

def get_active_symbols():
    """Разовый REST-запрос при старте для фильтрации топ-монет по обороту"""
    url = "https://api.bybit.com/v5/market/tickers?category=linear"
    try:
        res = requests.get(url, timeout=10).json()
        if res.get("retCode") == 0:
            return [
                item["symbol"] for item in res["result"]["list"]
                if item["symbol"].endswith("USDT") and float(item.get("turnover24h", 0)) >= MIN_TURNOVER_24H
            ]
    except Exception as e:
        print(f"Ошибка загрузки тикеров: {e}")
    return []

async def subscribe_topics(ws, topics):
    """Bybit принимает пачки максимум по 10 топиков за раз"""
    for i in range(0, len(topics), 10):
        chunk = topics[i:i+10]
        sub_msg = {"op": "subscribe", "args": chunk}
        await ws.send(json.dumps(sub_msg))
        await asyncio.sleep(0.05)

async def handle_liquidation(data, ws):
    """Обработка живых ликвидаций"""
    # data: {'symbol': '...', 'side': 'Buy'/'Sell', 'size': '...', 'price': '...', 'updatedTime': ...}
    symbol = data.get("symbol")
    side = data.get("side") # Buy = ликвидация шорта, Sell = ликвидация лонга
    
    if side != "Buy": 
        return  # Нас интересуют только ликвидированные шорты (топливо для разворота вниз)

    price = float(data.get("price", 0))
    size = float(data.get("size", 0))
    vol_usd = price * size
    now = time.time()

    if symbol not in liquidations_log:
        liquidations_log[symbol] = []
    
    liquidations_log[symbol].append((now, vol_usd))

    # Очистка устаревших записей за пределами окна
    liquidations_log[symbol] = [item for item in liquidations_log[symbol] if now - item[0] <= LIQ_WINDOW_SEC]
    total_liq = sum(item[1] for item in liquidations_log[symbol])

    # Если набрался нужный объем ликвидаций и монеты еще нет в работе
    if total_liq >= MIN_LIQ_VOLUME_USD and symbol not in watchlist:
        watchlist[symbol] = {
            "pump_high": price,
            "liq_total": total_liq,
            "added_at": now
        }
        print(f"🔥 ВСПЛЕСК ЛИКВИДАЦИЙ: {symbol} на ${total_liq:,.0f}! Подписка на M1...")
        # Динамически подписываемся на свечи M1 этой монеты
        await ws.send(json.dumps({"op": "subscribe", "args": [f"kline.1.{symbol}"]}))

async def handle_kline(data):
    """Обработка минутных свечей для поиска слома (CHoCH)"""
    # topic: kline.1.SYMBOL
    topic = data.get("topic", "")
    symbol = topic.split(".")[-1]
    
    if symbol not in watchlist:
        return

    candle_data = data["data"][0]
    is_closed = candle_data.get("confirm", False) # True только когда минута закрылась
    
    c_open = float(candle_data["open"])
    c_high = float(candle_data["high"])
    c_low = float(candle_data["low"])
    c_close = float(candle_data["close"])
    c_vol = float(candle_data["volume"])

    info = watchlist[symbol]
    if c_high > info["pump_high"]:
        info["pump_high"] = c_high

    if not is_closed:
        return

    # Запись в историю закрытых M1
    if symbol not in m1_history:
        m1_history[symbol] = []
    m1_history[symbol].append({
        "open": c_open, "high": c_high, "low": c_low, "close": c_close, "vol": c_vol
    })

    # Нам нужно хотя бы 6 закрытых минуток для определения локального свинга
    if len(m1_history[symbol]) < 6:
        return

    history = m1_history[symbol][:-1]
    trigger = m1_history[symbol][-1]

    # Свинговый минимум последних 5 минут
    swing_low = min(b["low"] for b in history[-5:])
    avg_vol = sum(b["volume"] for b in history[-10:]) / len(history[-10:])

    # КРИТЕРИИ CHoCH:
    # 1. Свеча закрылась красной
    # 2. Тело закрылось ниже свинга (слом поддержки)
    # 3. Объем слома выше среднего
    if (trigger["close"] < swing_low and 
        trigger["close"] < trigger["open"] and 
        trigger["vol"] >= avg_vol * M1_VOLUME_MULT):

        now_ts = int(time.time())
        event_key = (symbol, now_ts // 60)

        if event_key not in notified_events:
            notified_events.add(event_key)
            stop_loss = info["pump_high"]
            curr_price = trigger["close"]
            risk_pct = ((stop_loss - curr_price) / curr_price) * 100

            send_tg(
                f"⚡ <b>WEBSOCKET: СЛОМ ПОСЛЕ ЛИКВИДАЦИЙ (SHORT)</b>\n\n"
                f"🪙 <b>Пара:</b> <code>{symbol}</code>\n"
                f"• <b>Ликвидировано шортов:</b> <code>${info['liq_total']:,.0f}</code>\n"
                f"• Пробит свинговый Low (M1): <code>{swing_low}</code>\n"
                f"• Текущая цена (Вход): <code>{curr_price}</code>\n"
                f"• Всплеск объема M1: <code>{trigger['vol']/avg_vol:.1f}x</code>\n"
                f"• <b>Стоп (Пик ликвидаций):</b> <code>{stop_loss}</code> (риск: <code>{risk_pct:.2f}%</code>)\n"
                f"• <b>Тейк-профит:</b> <b>+2.0%</b>\n\n"
                f"💡 <i>Маркетмейкер собрал шорт-ликвидность. На минутке продавцы подтвердили перехват.</i>\n"
                f"🔗 <a href='https://www.bybit.com/trade/usdt/{symbol}'>Bybit</a>"
            )
            # Убираем из наблюдения после отправки
            watchlist.pop(symbol, None)

async def cleanup_loop():
    """Фоновая очистка протухших монет из watchlist"""
    while True:
        await asyncio.sleep(30)
        now = time.time()
        expired = [s for s, info in watchlist.items() if now - info["added_at"] > WATCH_EXPIRE_SEC]
        for s in expired:
            watchlist.pop(s, None)
            m1_history.pop(s, None)

async def ws_main():
    symbols = get_active_symbols()
    print(f"✓ Запуск WebSocket. Мониторинг ликвидаций для {len(symbols)} пар...")

    liq_topics = [f"liquidation.{s}" for s in symbols]

    while True:
        try:
            async with websockets.connect(BYBIT_WS_URL, ping_interval=20, ping_timeout=10) as ws:
                print("✓ Соединение с Bybit WebSocket установлено.")
                await subscribe_topics(ws, liq_topics)

                async for message in ws:
                    res = json.loads(message)
                    topic = res.get("topic", "")

                    if topic.startswith("liquidation."):
                        await handle_liquidation(res.get("data", {}), ws)
                    elif topic.startswith("kline.1."):
                        await handle_kline(res)

        except Exception as e:
            print(f"⚠️ Ошибка WS / разрыв соединения: {e}. Переподключение через 5 сек...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(cleanup_loop())
    loop.run_until_complete(ws_main())
