import asyncio
import json
import os
import time
import requests
import websockets

# --- НАСТРОЙКИ TELEGRAM ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5296533274")

# --- ПАРАМЕТРЫ ФИЛЬТРАЦИИ ---
MIN_TURNOVER_24H = 10_000   # Порог оборота ($10M+ отсекает мусор)
LIQ_WINDOW_SEC = 120            # Окно накопления ликвидаций: 2 минуты
MIN_LIQ_VOLUME_USD = 15_000    # Сумма ликвидаций шортов для триггера ($50k)
COOLDOWN_SEC = 15 * 60          # 15 минут кулдаун на повторный алерт по одной монете
TOPICS_PER_CONNECTION = 25      # Лимит подписок на один сокет (защита от банов Bybit)

BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"

liquidations_log = {}
last_alert_time = {}

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
        print(f"Ошибка TG: {e}")

def get_all_active_symbols():
    """Загружает абсолютно ВСЕ бессрочные контракты с оборотом > $10M"""
    url = "https://api.bybit.com/v5/market/tickers?category=linear"
    try:
        res = requests.get(url, timeout=10).json()
        if res.get("retCode") == 0:
            symbols = [
                i["symbol"] for i in res["result"]["list"]
                if i["symbol"].endswith("USDT") and float(i.get("turnover24h", 0)) >= MIN_TURNOVER_24H
            ]
            return sorted(symbols)
    except Exception as e:
        print(f"Ошибка загрузки тикеров: {e}")
    return []

async def handle_liquidation(data):
    symbol = data.get("symbol")
    side = data.get("side")

    # Нас интересуют только ликвидированные шортисты
    if side != "Buy":
        return

    price = float(data.get("price", 0))
    size = float(data.get("size", 0))
    vol_usd = price * size
    now = time.time()

    if symbol not in liquidations_log:
        liquidations_log[symbol] = []

    liquidations_log[symbol].append((now, vol_usd, price))

    # Срезаем старые записи за пределами 2 минут
    liquidations_log[symbol] = [i for i in liquidations_log[symbol] if now - i[0] <= LIQ_WINDOW_SEC]

    total_liq_usd = sum(i[1] for i in liquidations_log[symbol])
    max_price = max(i[2] for i in liquidations_log[symbol])

    if total_liq_usd >= MIN_LIQ_VOLUME_USD:
        last_sent = last_alert_time.get(symbol, 0)
        if now - last_sent >= COOLDOWN_SEC:
            last_alert_time[symbol] = now
            print(f"🚨 [СИГНАЛ] {symbol}: Ликвидаций на ${total_liq_usd:,.0f}")
            send_tg(
                f"🩸 <b>ВСПЛЕСК ЛИКВИДАЦИЙ ШОРТОВ: {symbol}</b>\n\n"
                f"• Объём ликвидаций (2м): <code>${total_liq_usd:,.0f}</code>\n"
                f"• Текущая цена: <code>{price}</code>\n"
                f"• Пик выноса: <code>{max_price}</code>\n\n"
                f"💡 <i>Жди слом структуры (CHoCH) на M1/M5 вниз с повышенным объёмом. Стоп за {max_price}.</i>\n"
                f"🔗 <a href='https://www.bybit.com/trade/usdt/{symbol}'>Bybit</a> | "
                f"<a href='https://www.coinglass.com/tv/Bybit_{symbol}'>CoinGlass</a>"
            )

async def ws_worker(worker_id: int, symbols_chunk: list):
    """Отдельный воркер, обслуживающий свою пачку альткоинов"""
    topics = [f"liquidation.{s}" for s in symbols_chunk]

    while True:
        try:
            async with websockets.connect(BYBIT_WS_URL, ping_interval=20, ping_timeout=10) as ws:
                # Подписываемся пачками по 10 штук
                for i in range(0, len(topics), 10):
                    batch = topics[i:i+10]
                    await ws.send(json.dumps({"op": "subscribe", "args": batch}))
                    await asyncio.sleep(0.05)

                print(f"✓ Воркер #{worker_id} слушает {len(symbols_chunk)} монет.")

                async for msg in ws:
                    res = json.loads(msg)
                    topic = res.get("topic", "")
                    if topic.startswith("liquidation."):
                        await handle_liquidation(res.get("data", {}))

        except Exception as e:
            print(f"⚠️ Воркер #{worker_id}: разрыв соединения ({e}). Реконнект через 5 сек...")
            await asyncio.sleep(5)

async def main():
    symbols = get_all_active_symbols()
    print(f"✓ Найдено {len(symbols)} активных альткоинов с оборотом > ${MIN_TURNOVER_24H/1e6:.0f}M.")

    # Делим весь список монет на группы по 25 штук
    chunks = [symbols[i:i + TOPICS_PER_CONNECTION] for i in range(0, len(symbols), TOPICS_PER_CONNECTION)]
    print(f"✓ Создано {len(chunks)} параллельных сокет-каналов для покрытия всего рынка.")

    tasks = [ws_worker(idx + 1, chunk) for idx, chunk in enumerate(chunks)]
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
