import os
import time
import asyncio
import logging
import aiohttp
import ccxt.async_support as ccxt
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== НАСТРОЙКИ ====================
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
if not TELEGRAM_BOT_TOKEN:
    print("✗ Ошибка: TELEGRAM_BOT_TOKEN не найден в переменных окружения!")
    exit(1)

MIN_PROFIT_PCT = 3.0           # Минимальный порог чистой прибыли (%)
TRADE_AMOUNT_USD = 500.0       # Сумма сделки для расчета ($)
CHECK_INTERVAL_SECONDS = 15    # Интервал между кругами сканирования (сек)

# Комиссионные издержки
CEX_TAKER_FEE = 0.001          # 0.1% комиссия MEXC
DEX_SWAP_FEE = 0.003           # 0.3% комиссия DEX
ESTIMATED_GAS_USD = 0.05       # Средний газ в L2/Solana ($0.15)
ESTIMATED_WITHDRAW_FEE_USD = 0.10 # Комиссия за вывод с CEX

mexc = ccxt.mexc({'enableRateLimit': True})
LAST_CHAT_ID = None

# Кэш защиты от спама
SEEN_OPPORTUNITIES = {}
ALERT_COOLDOWN_SECONDS = 600

CURRENCIES_CACHE = None
CURRENCIES_CACHE_TIME = 0


async def get_mexc_currencies():
    """Запрашивает информацию о монетах и сетях вывода с MEXC"""
    global CURRENCIES_CACHE, CURRENCIES_CACHE_TIME
    now = time.time()
    if not CURRENCIES_CACHE or (now - CURRENCIES_CACHE_TIME > 60):
        try:
            CURRENCIES_CACHE = await mexc.fetch_currencies()
            CURRENCIES_CACHE_TIME = now
        except Exception as e:
            logger.error(f"Ошибка получения валют MEXC: {e}")
            if not CURRENCIES_CACHE:
                CURRENCIES_CACHE = {}
    return CURRENCIES_CACHE


async def is_withdraw_enabled(symbol: str) -> tuple[bool, str]:
    """
    Проверяет, открыт ли вывод для токена на MEXC хотя бы в одной быстрой сети.
    Возвращает (Status, NetworkName).
    """
    try:
        currencies = await get_mexc_currencies()
        if symbol not in currencies:
            return False, ""

        networks_info = currencies[symbol].get('networks', {})
        fast_networks = ['arbitrum', 'base', 'solana', 'optimism', 'polygon', 'bsc']

        for net_code, net_data in networks_info.items():
            net_clean = net_code.lower()
            for fast_net in fast_networks:
                if fast_net in net_clean:
                    if net_data.get('withdraw', False) and net_data.get('active', True):
                        return True, fast_net.upper()
    except Exception as e:
        logger.error(f"Ошибка проверки вывода MEXC для {symbol}: {e}")

    return False, ""


async def get_dex_prices_dexscreener(session: aiohttp.ClientSession, symbol: str):
    """
    Запрашивает все активные DEX-пулы по тикеру токена через DexScreener API.
    Это позволяет мгновенно находить цену токена на любых DEX.
    """
    url = f"https://api.dexscreener.com/latest/dex/search?q={symbol}"
    results = []
    try:
        async with session.get(url, timeout=5) as resp:
            if resp.status == 200:
                data = await resp.json()
                pairs = data.get('pairs', [])
                for pair in pairs[:5]:  # Берем первые 5 самых ликвидных пулов
                    base_symbol = pair.get('baseToken', {}).get('symbol', '').upper()
                    if base_symbol == symbol.upper():
                        liquidity = float(pair.get('liquidity', {}).get('usd', 0) or 0)
                        price_usd = float(pair.get('priceUsd', 0) or 0)
                        
                        # Фильтруем пулы с ликвидностью от $3,000
                        if liquidity >= 3000 and price_usd > 0:
                            results.append({
                                'dex_price': price_usd,
                                'network': pair.get('chainId', '').upper(),
                                'dex_name': pair.get('dexId', ''),
                                'pair_address': pair.get('pairAddress', ''),
                                'contract': pair.get('baseToken', {}).get('address', ''),
                                'liquidity': liquidity
                            })
    except Exception as e:
        logger.error(f"Ошибка DexScreener для {symbol}: {e}")
    return results


async def auto_scan_arbitrage(session: aiohttp.ClientSession):
    """Сканирует активные пары MEXC и ищет расхождения на DEX"""
    opportunities = []
    current_time = time.time()

    try:
        # 1. Запрашиваем тикеры с MEXC
        mexc_tickers = await mexc.fetch_tickers()
    except Exception as e:
        logger.error(f"Ошибка получения тикеров MEXC: {e}")
        return []

    # Отбираем волатильные пары к USDT
    active_symbols = [
        sym.split('/')[0] for sym in mexc_tickers.keys() 
        if sym.endswith('/USDT')
    ]

    # Для примера сканируем срезами по 15 монет за один проход
    # (чтобы соблюдать лимиты API)
    for symbol in active_symbols[:30]:
        mexc_pair = f"{symbol}/USDT"
        mexc_data = mexc_tickers.get(mexc_pair, {})
        mexc_ask = mexc_data.get('ask')  # Цена покупки на MEXC

        if not mexc_ask or mexc_ask <= 0:
            continue

        # 2. Ищем этот токен на DEX через DexScreener
        dex_matches = await get_dex_prices_dexscreener(session, symbol)

        for dex_item in dex_matches:
            pool_addr = dex_item['pair_address']

            # Проверка кэша алертов
            if pool_addr in SEEN_OPPORTUNITIES:
                if current_time - SEEN_OPPORTUNITIES[pool_addr] < ALERT_COOLDOWN_SECONDS:
                    continue

            dex_bid = dex_item['dex_price']

            # 3. Расчет чистой математики
            tokens_bought = (TRADE_AMOUNT_USD / mexc_ask) * (1 - CEX_TAKER_FEE)
            gross_dex = tokens_bought * dex_bid
            net_dex = gross_dex * (1 - DEX_SWAP_FEE) - ESTIMATED_GAS_USD - ESTIMATED_WITHDRAW_FEE_USD
            net_profit_usd = net_dex - TRADE_AMOUNT_USD
            net_profit_pct = (net_profit_usd / TRADE_AMOUNT_USD) * 100

            # 4. Если профит подходит — проверяем статус вывода на MEXC
            if net_profit_pct >= MIN_PROFIT_PCT:
                withdraw_ok, network_name = await is_withdraw_enabled(symbol)
                
                if not withdraw_ok:
                    continue

                SEEN_OPPORTUNITIES[pool_addr] = current_time
                opportunities.append({
                    'token': symbol,
                    'network': network_name,
                    'dex_name': dex_item['dex_name'].upper(),
                    'mexc_price': mexc_ask,
                    'dex_price': dex_bid,
                    'profit_usd': net_profit_usd,
                    'profit_pct': net_profit_pct,
                    'pool_address': pool_addr,
                    'contract': dex_item['contract'],
                    'liquidity': dex_item['liquidity']
                })

    return opportunities


async def background_scanner(application):
    """Фоновый поток сканирования"""
    await asyncio.sleep(2)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                opportunities = await auto_scan_arbitrage(session)
                if opportunities and LAST_CHAT_ID:
                    for opp in opportunities:
                        msg = (
                            f"🚨 **АРБИТРАЖНАЯ СВЯЗКА: MEXC ➔ DEX**\n\n"
                            f"🪙 **Токен:** `{opp['token']}` (Сеть: `{opp['network']}`)\n"
                            f"🟢 **Купить на MEXC:** `${opp['mexc_price']:.6f}`\n"
                            f"🔵 **Продать на DEX ({opp['dex_name']}):** `${opp['dex_price']:.6f}`\n\n"
                            f"✅ **Вывод с MEXC:** `Открыт`\n"
                            f"💧 **Ликвидность DEX:** `${opp['liquidity']:,.0f}`\n"
                            f"💵 **Депозит:** `${TRADE_AMOUNT_USD}`\n"
                            f"📈 **Чистый профит:** `+${opp['profit_usd']:.2f}` (`+{opp['profit_pct']:.2f}%`)\n\n"
                            f"📍 **Пул:** `{opp['pool_address']}`\n"
                            f"📝 **Контракт:** `{opp['contract']}`"
                        )
                        await application.bot.send_message(
                            chat_id=LAST_CHAT_ID, 
                            text=msg, 
                            parse_mode="Markdown"
                        )
            except Exception as e:
                logger.error(f"Ошибка в фоновом сканере: {e}")

            await asyncio.sleep(CHECK_INTERVAL_SECONDS)


# === Телеграм Команды ===
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LAST_CHAT_ID
    LAST_CHAT_ID = update.effective_chat.id
    await update.message.reply_text(
        f"⚡ **Сканер MEXC ↔ DEX (Обратный поиск) запущен!**\n\n"
        f"• Поиск спредов по всем активным монетам MEXC\n"
        f"• Порог чистой прибыли: `{MIN_PROFIT_PCT}%`\n"
        f"• Проверка открытого вывода на MEXC: `Включена`\n\n"
        f"Изменить порог: `/set 4.0`",
        parse_mode="Markdown"
    )


async def set_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MIN_PROFIT_PCT, LAST_CHAT_ID
    LAST_CHAT_ID = update.effective_chat.id

    if not context.args:
        await update.message.reply_text(f"Текущий порог: `{MIN_PROFIT_PCT}%`")
        return

    try:
        MIN_PROFIT_PCT = float(context.args[0].replace(',', '.'))
        await update.message.reply_text(f"✅ Новый порог чистой прибыли: `{MIN_PROFIT_PCT}%`")
    except ValueError:
        await update.message.reply_text("❌ Введите число, например `/set 2.5`")


async def main():
    try:
        app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(CommandHandler("set", set_command))

        asyncio.create_task(background_scanner(app))

        await app.initialize()
        await app.start()
        await app.updater.start_polling()

        stop_signal = asyncio.Event()
        await stop_signal.wait()
    finally:
        await mexc.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен.")
