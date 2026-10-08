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

# ==================== НАСТРОЙКИ (ПО УМОЛЧАНИЮ) ====================
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
if not TELEGRAM_BOT_TOKEN:
    print("✗ Ошибка: TELEGRAM_BOT_TOKEN не найден в переменных окружения!")
    exit(1)

MIN_PROFIT_PCT = 3.0           # Минимальный чистый профит (%)
TRADE_AMOUNT_USD = 500.0       # Сумма сделки для расчета ($)
CHECK_INTERVAL_SECONDS = 10    # Интервал проверки (сек)

# Коэффициенты комиссий
CEX_TAKER_FEE = 0.001          # 0.1% комиссия CEX (MEXC)
DEX_SWAP_FEE = 0.003           # 0.3% комиссия DEX
ESTIMATED_GAS_USD = 0.15       # Примерный газ в L2/Solana ($0.15)
ESTIMATED_WITHDRAW_FEE_USD = 0.50 # Средняя комиссия вывода с MEXC в L2

# Целевые быстрые сети
NETWORKS = ['arbitrum', 'base', 'solana', 'optimism']

# Маппинг названий сетей GeckoTerminal ➔ MEXC API
NETWORK_MAPPING = {
    'arbitrum': ['arbitrum', 'arb', 'arbitrum one'],
    'base': ['base'],
    'solana': ['solana', 'sol'],
    'optimism': ['optimism', 'op']
}

mexc = ccxt.mexc({'enableRateLimit': True})
LAST_CHAT_ID = None

# Кэш для повторных алертов и данных валют MEXC
SEEN_OPPORTUNITIES = {}
ALERT_COOLDOWN_SECONDS = 600

CURRENCIES_CACHE = None
CURRENCIES_CACHE_TIME = 0
CACHE_TTL_SECONDS = 60  # Обновляем структуру валют раз в минуту


async def get_mexc_currencies():
    """Запрашивает структуры монет MEXC с кэшированием"""
    global CURRENCIES_CACHE, CURRENCIES_CACHE_TIME
    now = time.time()
    if not CURRENCIES_CACHE or (now - CURRENCIES_CACHE_TIME > CACHE_TTL_SECONDS):
        try:
            CURRENCIES_CACHE = await mexc.fetch_currencies()
            CURRENCIES_CACHE_TIME = now
        except Exception as e:
            logger.error(f"Ошибка получения валют MEXC: {e}")
            if not CURRENCIES_CACHE:
                CURRENCIES_CACHE = {}
    return CURRENCIES_CACHE


async def is_withdraw_enabled(symbol: str, target_network: str) -> bool:
    """
    Проверяет через API MEXC, разрешен ли вывод токена в конкретной сети.
    """
    try:
        currencies = await get_mexc_currencies()
        if symbol not in currencies:
            return False

        currency_info = currencies[symbol]
        networks_info = currency_info.get('networks', {})
        target_aliases = NETWORK_MAPPING.get(target_network.lower(), [target_network.lower()])

        # Ищем совпадение нужной сети в структуре MEXC
        for net_code, net_data in networks_info.items():
            net_code_clean = net_code.lower()
            net_name_clean = str(net_data.get('name', '')).lower()

            # Проверяем совпадение алиасов сети
            if any(alias in net_code_clean or alias in net_name_clean for alias in target_aliases):
                # Флаг доступности вывода
                can_withdraw = net_data.get('withdraw', False)
                active = net_data.get('active', True)
                return bool(can_withdraw and active)

    except Exception as e:
        logger.error(f"Ошибка проверки вывода MEXC для {symbol} ({target_network}): {e}")

    return False


async def get_dex_new_pools(session: aiohttp.ClientSession, network: str):
    """Получает самые свежие пулы с GeckoTerminal"""
    url = f"https://api.geckoterminal.com/api/v2/networks/{network}/new_pools"
    pools = []
    try:
        async with session.get(url, timeout=5) as resp:
            if resp.status == 200:
                data = await resp.json()
                for item in data.get('data', []):
                    attr = item['attributes']
                    
                    reserve = float(attr.get('reserve_in_usd', 0) or 0)
                    volume_24h = float(attr.get('volume_usd', {}).get('h24', 0) or 0)
                    
                    if reserve >= 3000:
                        name = attr.get('name', '')
                        base_symbol = name.split('/')[0].strip().upper()
                        price_usd = float(attr.get('base_token_price_usd', 0) or 0)
                        pool_address = attr.get('address')
                        
                        rel = item.get('relationships', {})
                        base_token_id = rel.get('base_token', {}).get('data', {}).get('id', '')
                        contract_address = base_token_id.split('_')[-1] if '_' in base_token_id else pool_address

                        if price_usd > 0:
                            pools.append({
                                'symbol': base_symbol,
                                'price_usd': price_usd,
                                'network': network,
                                'pool_address': pool_address,
                                'contract_address': contract_address,
                                'reserve': reserve,
                                'volume_24h': volume_24h
                            })
    except Exception as e:
        logger.error(f"Ошибка запроса DEX пулов для {network}: {e}")
    return pools


async def auto_scan_arbitrage(session: aiohttp.ClientSession):
    """Сканирует новые пулы и проверяет статус вывода перед формированием сигнала"""
    opportunities = []
    current_time = time.time()
    
    try:
        mexc_tickers = await mexc.fetch_tickers()
    except Exception as e:
        logger.error(f"Ошибка получения тикеров MEXC: {e}")
        return []

    dex_pools = []
    for net in NETWORKS:
        net_pools = await get_dex_new_pools(session, net)
        dex_pools.extend(net_pools)

    for dex_item in dex_pools:
        symbol = dex_item['symbol']
        pool_addr = dex_item['pool_address']
        network = dex_item['network']

        if pool_addr in SEEN_OPPORTUNITIES:
            if current_time - SEEN_OPPORTUNITIES[pool_addr] < ALERT_COOLDOWN_SECONDS:
                continue

        mexc_pair = f"{symbol}/USDT"

        if mexc_pair in mexc_tickers:
            mexc_data = mexc_tickers[mexc_pair]
            mexc_ask = mexc_data.get('ask')

            if not mexc_ask or mexc_ask <= 0:
                continue

            dex_bid = dex_item['price_usd']

            # Расчет математики профита
            tokens_bought = (TRADE_AMOUNT_USD / mexc_ask) * (1 - CEX_TAKER_FEE)
            gross_dex = tokens_bought * dex_bid
            net_dex = gross_dex * (1 - DEX_SWAP_FEE) - ESTIMATED_GAS_USD - ESTIMATED_WITHDRAW_FEE_USD
            net_profit_usd = net_dex - TRADE_AMOUNT_USD
            net_profit_pct = (net_profit_usd / TRADE_AMOUNT_USD) * 100

            if net_profit_pct >= MIN_PROFIT_PCT:
                # 🛑 КЛЮЧЕВАЯ ПРОВЕРКА: Проверяем доступность вывода на MEXC
                withdraw_status = await is_withdraw_enabled(symbol, network)
                
                if not withdraw_status:
                    logger.info(f"Пропущено {symbol} ({network}): Вывод на MEXC приостановлен.")
                    continue

                SEEN_OPPORTUNITIES[pool_addr] = current_time
                opportunities.append({
                    'token': symbol,
                    'network': network.upper(),
                    'mexc_price': mexc_ask,
                    'dex_price': dex_bid,
                    'profit_usd': net_profit_usd,
                    'profit_pct': net_profit_pct,
                    'pool_address': pool_addr,
                    'contract': dex_item['contract_address'],
                    'reserve': dex_item['reserve']
                })

    return opportunities


async def background_scanner(application):
    """Фоновый поток авто-сканирования"""
    await asyncio.sleep(2)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                opportunities = await auto_scan_arbitrage(session)
                if opportunities and LAST_CHAT_ID:
                    for opp in opportunities:
                        msg = (
                            f"🔥 **СВЕЖИЙ ЛИСТИНГ: CEX ➔ DEX** 🔥\n\n"
                            f"🪙 **Токен:** `{opp['token']}` (Сеть: `{opp['network']}`)\n"
                            f"🟢 **Купить на MEXC:** `${opp['mexc_price']:.6f}`\n"
                            f"🔵 **Продать на DEX:** `${opp['dex_price']:.6f}`\n\n"
                            f"✅ **Вывод с MEXC:** `Открыт (Verified)`\n"
                            f"💧 **Ликвидность DEX:** `${opp['reserve']:,.0f}`\n"
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
                logger.error(f"Ошибка в авто-сканере: {e}")

            await asyncio.sleep(CHECK_INTERVAL_SECONDS)


# === Телеграм Команды ===
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LAST_CHAT_ID
    LAST_CHAT_ID = update.effective_chat.id
    await update.message.reply_text(
        f"⚡ **Сканер MEXC ↔ DEX (с проверкой вывода) запущен!**\n\n"
        f"• Проверка вывода монет через API MEXC: `Включена`\n"
        f"• Порог чистой прибыли: `{MIN_PROFIT_PCT}%`\n"
        f"• Интервал сканирования: `{CHECK_INTERVAL_SECONDS}s`\n"
        f"• Сети: `Arbitrum`, `Base`, `Solana`, `Optimism`\n\n"
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
