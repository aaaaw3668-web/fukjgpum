import asyncio
import logging
import os
import sys
from typing import Dict, List, Optional
import aiohttp
from dotenv import load_dotenv

# Загружаем переменные из .env файла, если он существует локально
load_dotenv()

# ==================== НАСТРОЙКИ ====================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not TELEGRAM_BOT_TOKEN:
    print("✗ Ошибка: TELEGRAM_BOT_TOKEN не найден в переменных окружения!")
    sys.exit(1)

TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
if not TELEGRAM_CHAT_ID:
    print("✗ Ошибка: TELEGRAM_CHAT_ID не найден в переменных окружения!")
    sys.exit(1)

# Параметры торговой стратегии
TIMEFRAME = os.environ.get("TIMEFRAME", "15m")
PUMP_THRESHOLD_PCT = float(os.environ.get("PUMP_THRESHOLD_PCT", 5.0))     # Мин. памп перед разворотом (%)
DUMP_REVERSAL_PCT = float(os.environ.get("DUMP_REVERSAL_PCT", 1.5))       # Откат от локального пика (%)
VOLUME_SPIKE_RATIO = float(os.environ.get("VOLUME_SPIKE_RATIO", 1.3))     # Превышение объёма над средним
OI_DROP_THRESHOLD_PCT = float(os.environ.get("OI_DROP_THRESHOLD_PCT", 2.0)) # Падение ОИ (%)

CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", 60))        # Интервал цикла (сек)
# ===================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

BINANCE_FUTURES_URL = "https://fapi.binance.com"


async def send_telegram_alert(session: aiohttp.ClientSession, message: str) -> None:
    """Отправка уведомления в Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        async with session.post(url, json=payload, timeout=10) as resp:
            if resp.status != 200:
                logging.error(f"Ошибка Telegram API: {await resp.text()}")
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление: {e}")


async def get_active_usdt_pairs(session: aiohttp.ClientSession) -> List[str]:
    """Получение списка активных бессрочных USDT-пар."""
    url = f"{BINANCE_FUTURES_URL}/fapi/v1/exchangeInfo"
    async with session.get(url, timeout=10) as resp:
        data = await resp.json()
        return [
            s["symbol"]
            for s in data["symbols"]
            if s["contractType"] == "PERPETUAL"
            and s["quoteAsset"] == "USDT"
            and s["status"] == "TRADING"
        ]


async def fetch_klines(session: aiohttp.ClientSession, symbol: str, limit: int = 15) -> Optional[List[dict]]:
    """Получение свечей с Binance Futures."""
    url = f"{BINANCE_FUTURES_URL}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": TIMEFRAME, "limit": limit}
    try:
        async with session.get(url, params=params, timeout=5) as resp:
            data = await resp.json()
            if isinstance(data, list):
                return [
                    {
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                    }
                    for k in data
                ]
    except Exception:
        pass
    return None


async def fetch_oi_history(session: aiohttp.ClientSession, symbol: str, limit: int = 5) -> Optional[List[float]]:
    """Получение истории открытого интереса (Open Interest)."""
    url = f"{BINANCE_FUTURES_URL}/futures/data/openInterestHist"
    params = {"symbol": symbol, "period": TIMEFRAME, "limit": limit}
    try:
        async with session.get(url, params=params, timeout=5) as resp:
            data = await resp.json()
            if isinstance(data, list) and len(data) > 0:
                return [float(entry["sumOpenInterestValue"]) for entry in data]
    except Exception:
        pass
    return None


async def analyze_symbol(session: aiohttp.ClientSession, symbol: str, cooldowns: Dict[str, float]) -> None:
    """Анализ условий: Памп -> Разворот + Рост объема на падении + Сброс ОИ."""
    klines = await fetch_klines(session, symbol, limit=12)
    if not klines or len(klines) < 6:
        return

    # 1. Проверка структуры цены: памп и начало отката
    current_price = klines[-1]["close"]
    prev_high = max(k["high"] for k in klines[:-1])
    min_base_price = min(k["low"] for k in klines[:6])

    pump_pct = ((prev_high - min_base_price) / min_base_price) * 100
    reversal_pct = ((prev_high - current_price) / prev_high) * 100

    if pump_pct < PUMP_THRESHOLD_PCT or reversal_pct < DUMP_REVERSAL_PCT:
        return

    # 2. Проверка объёма: свеча падения и объём выше среднего
    current_candle = klines[-1]
    is_red_candle = current_candle["close"] < current_candle["open"]
    if not is_red_candle:
        return

    avg_volume = sum(k["volume"] for k in klines[-6:-1]) / 5
    if current_candle["volume"] < avg_volume * VOLUME_SPIKE_RATIO:
        return

    # 3. Проверка Open Interest: падение ОИ
    oi_history = await fetch_oi_history(session, symbol, limit=4)
    if not oi_history or len(oi_history) < 2:
        return

    max_oi = max(oi_history[:-1])
    current_oi = oi_history[-1]
    oi_drop_pct = ((max_oi - current_oi) / max_oi) * 100

    if oi_drop_pct < OI_DROP_THRESHOLD_PCT:
        return

    # Кулдаун 15 минут (900 сек), чтобы не сыпать одинаковыми алертами
    now = asyncio.get_event_loop().time()
    if symbol in cooldowns and (now - cooldowns[symbol]) < 900:
        return

    cooldowns[symbol] = now

    msg = (
        f"🚨 *Сигнал: Памп -> Разворот / Лонг-сквиз*\n"
        f"🪙 *Пара:* `{symbol}`\n"
        f"📈 *Памп:* `+{pump_pct:.2f}%`\n"
        f"📉 *Откат от хая:* `-{reversal_pct:.2f}%`\n"
        f"📊 *Всплеск объёма:* `x{current_candle['volume'] / avg_volume:.2f}` от среднего\n"
        f"📉 *Сброс ОИ:* `-{oi_drop_pct:.2f}%` (выход лонгов/ликвидации)\n"
        f"💲 *Текущая цена:* `{current_price}`"
    )

    logging.info(f"Сработал сигнал по {symbol}")
    await send_telegram_alert(session, msg)


async def main():
    async with aiohttp.ClientSession() as session:
        logging.info("Скринер запущен. Получение списка пар Binance Futures...")
        symbols = await get_active_usdt_pairs(session)
        logging.info(f"Найдено {len(symbols)} пар.")

        cooldowns: Dict[str, float] = {}

        while True:
            # Сканируем пачками по 20 пар с паузой 0.5с для соблюдения rate limits
            batch_size = 20
            for i in range(0, len(symbols), batch_size):
                batch = symbols[i:i + batch_size]
                tasks = [analyze_symbol(session, sym, cooldowns) for sym in batch]
                await asyncio.gather(*tasks)
                await asyncio.sleep(0.5)

            await asyncio.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nСкринер остановлен.")
