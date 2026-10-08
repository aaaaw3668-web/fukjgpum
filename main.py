import os
import asyncio
import logging
import time
import ccxt.async_support as ccxt
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
if not TELEGRAM_BOT_TOKEN:
    print("✗ Ошибка: TELEGRAM_BOT_TOKEN не найден!")
    exit(1)

FUNDING_THRESHOLD_PCT = -1.0
CHECK_INTERVAL_SECONDS = 300          # 5 минут — безопасно для rate limit
ALERT_COOLDOWN_SECONDS = 3600
RATE_CHANGE_THRESHOLD = 0.3           # Присылать повторно, если ставка упала на 0.3%

subscribers: set[int] = set()
# symbol -> {'ts': timestamp, 'rate': last_rate}
alert_cache: dict[str, dict] = {}

bybit = ccxt.bybit({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})


def get_minutes_to_next_funding(data: dict) -> float:
    next_time = data.get('fundingTimestamp') or data.get('nextFundingTime')
    if not next_time:
        return 0.0
    diff_ms = next_time - time.time() * 1000
    return max(0.0, diff_ms / 60000)


async def fetch_extreme_funding():
    """Сканирует USDT-перпетуалы Bybit на фандинг <= порога."""
    try:
        markets = await bybit.load_markets()
        symbols = [
            s for s, m in markets.items()
            if m.get('swap') and m.get('linear') and m.get('quote') == 'USDT' and m.get('active')
        ]

        # Батч-запрос фандинга (CCXT умеет принимать список)
        rates = await bybit.fetch_funding_rates(symbols)

        result = []
        for sym, data in rates.items():
            rate = data.get('fundingRate')
            if rate is None:
                continue
            rate_pct = float(rate) * 100
            if rate_pct <= FUNDING_THRESHOLD_PCT:
                result.append({
                    'symbol': sym,
                    'rate': rate_pct,
                    'mins_left': get_minutes_to_next_funding(data)
                })
        return result
    except Exception as e:
        logger.error(f"Ошибка запроса к Bybit: {e}")
        return []


def filter_alerts(alerts):
    """Пропускает алерт, если он новый ИЛИ ставка заметно усилилась."""
    now = time.time()
    fresh = []

    # Чистим устаревшее
    for k in [k for k, v in alert_cache.items() if now - v['ts'] > ALERT_COOLDOWN_SECONDS]:
        del alert_cache[k]

    for a in alerts:
        sym, rate = a['symbol'], a['rate']
        prev = alert_cache.get(sym)

        if prev is None:
            alert_cache[sym] = {'ts': now, 'rate': rate}
            fresh.append(a)
        elif rate <= prev['rate'] - RATE_CHANGE_THRESHOLD:
            # Фандинг стал ещё отрицательнее — шлём апдейт
            alert_cache[sym] = {'ts': now, 'rate': rate}
            fresh.append(a)

    return fresh


async def background_scanner(app):
    await asyncio.sleep(5)
    while True:
        logger.info("Сканирование фандинга Bybit...")
        alerts = await fetch_extreme_funding()

        if alerts:
            alerts.sort(key=lambda x: x['rate'])
            new_alerts = filter_alerts(alerts)

            if new_alerts and subscribers:
                msg = "🚨 **Экстремально отрицательный фандинг Bybit!** (≤ -1%)\n\n"
                for it in new_alerts:
                    msg += (
                        f"🔹 **{it['symbol']}**\n"
                        f"📉 Ставка: `{it['rate']:.4f}%`\n"
                        f"⏳ До выплаты: ~`{it['mins_left']:.0f} мин`\n\n"
                    )
                for chat_id in list(subscribers):
                    try:
                        await app.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                    except Exception as e:
                        logger.error(f"Ошибка отправки {chat_id}: {e}")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscribers.add(update.effective_chat.id)
    await update.message.reply_text(
        "🤖 **Бот отслеживания фандинга Bybit запущен!**\n\n"
        "Уведомления приходят при ставке **≤ -1.0%** и её усилении.",
        parse_mode="Markdown"
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscribers.discard(update.effective_chat.id)
    await update.message.reply_text("🔕 Уведомления отключены. /start — включить снова.")


async def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("stop", stop_command))

    asyncio.create_task(background_scanner(app))

    print("Бот запущен...")
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await bybit.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен.")