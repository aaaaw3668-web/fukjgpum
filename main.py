import os
import re
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
import requests

# ==================== НАСТРОЙКИ ====================
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
if not TELEGRAM_BOT_TOKEN:
    print("✗ Ошибка: TELEGRAM_BOT_TOKEN не найден в переменных окружения!")
    exit(1)

# Пороги срабатывания (синхронный лонговый импульс)
PRICE_INCREASE_THRESHOLD = 1.5   # Рост цены от +1.5%
OI_INCREASE_THRESHOLD = 3.0      # Рост OI от +3.0%

TIME_WINDOW = 60 * 15              # Окно анализа: 5 минут (300 сек)
COOLDOWN_MINUTES = 10             # Пауза между алертами по одной монете
DAILY_ALERT_LIMIT = 100           # Суточный лимит уведомлений на одну монету

# Сессия для переиспользования соединений
session = requests.Session()

# База данных пользователей в памяти
users = {
    '5296533274': {
        'active': True,
        'alert_counts': {}
    }
}

# Хранилище истории и временных меток алертов
historical_data = {}
last_alert_time = {}              # { 'BTCUSDT': timestamp_последней_отправки }


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def get_ye_time():
    """Возвращает текущее время по Уфимскому времени (UTC+5)"""
    return datetime.now(timezone.utc) + timedelta(hours=5)


def get_alert_count(chat_id, symbol):
    if chat_id not in users:
        return 0
    return users[chat_id]['alert_counts'].get(symbol, 0)


def increment_alert_count(chat_id, symbol):
    if chat_id in users:
        users[chat_id]['alert_counts'][symbol] = get_alert_count(chat_id, symbol) + 1


def can_send_alert(chat_id, symbol):
    if chat_id not in users or not users[chat_id]['active']:
        return False
    if get_alert_count(chat_id, symbol) >= DAILY_ALERT_LIMIT:
        return False
    return True


def calculate_change(old, new):
    if old == 0:
        return 0.0
    return ((new - old) / old) * 100


def generate_links(symbol):
    encoded_symbol = urllib.parse.quote(symbol)
    return {
        'coinglass': f"https://www.coinglass.com/tv/Binance_{encoded_symbol}",
        'tradingview': f"https://www.tradingview.com/chart/?symbol=BYBIT%3A{encoded_symbol}",
        'binance': f"https://www.binance.com/ru/trade/{encoded_symbol}",
        'bybit': f"https://www.bybit.com/trade/usdt/{encoded_symbol}"
    }


# ==================== РАБОТА С TELEGRAM ====================
def send_telegram_notification(chat_id, message, symbol):
    if not can_send_alert(chat_id, symbol):
        return False

    increment_alert_count(chat_id, symbol)
    current_count = get_alert_count(chat_id, symbol)
    monospace_symbol = f"<code>{symbol}</code>"

    def wrap_numbers(text):
        return re.sub(r'(-?\d+(?:\.\d+)?%)', r'<code>\1</code>', text)

    message = wrap_numbers(message)
    links = generate_links(symbol)
    message_with_links = (
        f"{message}\n\n"
        f"🔗 <b>Быстрый анализ:</b>\n"
        f"• 📊 <a href='{links['coinglass']}'>Coinglass TV</a>\n"
        f"• 📈 <a href='{links['tradingview']}'>TradingView</a>\n"
        f"• 💰 <a href='{links['binance']}'>Binance</a>\n"
        f"• ⚡ <a href='{links['bybit']}'>Bybit</a>\n\n"
        f"📊 <b>Уведомлений по {monospace_symbol} за сегодня:</b> <code>{current_count}/{DAILY_ALERT_LIMIT}</code>"
    )

    message_with_links = message_with_links.replace(symbol, monospace_symbol)

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': message_with_links,
        'parse_mode': 'HTML',
        'disable_web_page_preview': False
    }
    try:
        response = session.post(url, json=payload, timeout=10)
        response.raise_for_status()
        print(f"✓ Уведомление отправлено для {symbol} пользователю {chat_id} ({current_count}/{DAILY_ALERT_LIMIT})")
        return True
    except Exception as e:
        print(f"✗ Ошибка отправки пользователю {chat_id}: {repr(e)}")
        return False


def broadcast_message(message):
    for chat_id in list(users.keys()):
        if users[chat_id]['active']:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML'}
            try:
                session.post(url, json=payload, timeout=10)
            except Exception as e:
                print(f"✗ Ошибка рассылки: {e}")


def handle_telegram_updates():
    last_update_id = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {'timeout': 20, 'offset': last_update_id + 1}
            response = session.get(url, params=params, timeout=25)
            data = response.json()

            if data.get('ok'):
                for update in data['result']:
                    last_update_id = update['update_id']
                    if 'message' not in update:
                        continue

                    message = update['message']
                    chat_id = str(message['chat']['id'])
                    text = message.get('text', '').strip().lower()

                    if chat_id not in users and text == '/start':
                        users[chat_id] = {'active': True, 'alert_counts': {}}
                        welcome_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                        payload = {
                            'chat_id': chat_id,
                            'text': f"✅ <b>Вы успешно подписались!</b>\nЛимит: <b>{DAILY_ALERT_LIMIT} в сутки</b>.\n⏰ Сброс лимитов в 5:00 по Уфимскому времени.",
                            'parse_mode': 'HTML'
                        }
                        try:
                            session.post(welcome_url, json=payload)
                        except Exception:
                            pass

                    elif text == '/stats':
                        counts = users.get(chat_id, {}).get('alert_counts', {})
                        stats_text = f"📊 <b>Статистика (Лимит: {DAILY_ALERT_LIMIT}):</b>\n\n"
                        if counts:
                            for sym, count in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:20]:
                                stats_text += f"• <code>{sym}</code>: {count}/{DAILY_ALERT_LIMIT}\n"
                        else:
                            stats_text += "Сегодня алертов не было."

                        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                        try:
                            session.post(url, json={'chat_id': chat_id, 'text': stats_text, 'parse_mode': 'HTML'})
                        except Exception:
                            pass
            time.sleep(3)
        except Exception as e:
            print(f"✗ Ошибка Long Polling Telegram: {e}")
            time.sleep(10)


def check_and_reset_at_midnight():
    """Сброс суточных счетчиков в 5:00 по Уфе (UTC+5)"""
    now = get_ye_time()
    reset_time = now.replace(hour=5, minute=0, second=0, microsecond=0)

    if now >= reset_time:
        reset_time = reset_time + timedelta(days=1)

    print(f"⏰ Следующий сброс лимитов в: {reset_time.strftime('%Y-%m-%d %H:%M:%S')} (Уфимское время)")

    while True:
        try:
            now = get_ye_time()
            if now >= reset_time:
                print(f"⏰ Наступило 5 утра по Уфимскому времени ({now}). Сброс лимитов...")

                for chat_id in users:
                    users[chat_id]['alert_counts'] = {}

                reset_message = (
                    "🔄 <b>Внимание! Наступило 5:00 по Уфимскому времени.</b>\n"
                    f"Суточные лимиты уведомлений (<code>{DAILY_ALERT_LIMIT}</code> на монету) успешно сброшены!"
                )
                broadcast_message(reset_message)

                reset_time = reset_time + timedelta(days=1)
                print(f"⏰ Следующий сброс лимитов в: {reset_time.strftime('%Y-%m-%d %H:%M:%S')} (Уфимское время)")

            time.sleep(30)
        except Exception as e:
            print(f"✗ Ошибка в потоке сброса лимитов: {e}")
            time.sleep(30)


# ==================== РАБОТА С BYBIT API ====================
def fetch_perpetual_symbols():
    url = "https://api.bybit.com/v5/market/instruments-info"
    params = {"category": "linear"}
    try:
        response = session.get(url, params=params, timeout=15)
        if response.status_code == 200:
            data = response.json()
            if data['retCode'] == 0:
                symbols = [item['symbol'] for item in data['result']['list'] if item['symbol'].endswith('USDT')]
                print(f"✓ Загружено {len(symbols)} USDT-символов")
                return symbols
    except Exception as e:
        print(f"✗ Ошибка получения символов: {e}")
    return []


def fetch_all_bybit_tickers():
    url = "https://api.bybit.com/v5/market/tickers"
    params = {"category": "linear"}
    try:
        response = session.get(url, params=params, timeout=15)
        if response.status_code == 200:
            data = response.json()
            if data['retCode'] == 0:
                return data['result']['list']
    except Exception as e:
        print(f"✗ Ошибка получения тикеров Bybit: {e}")
    return []


# ==================== ОСНОВНОЙ ЦИКЛ ====================
def main():
    print("=== Запуск мониторинга (Рост цены от +2.5% И Рост OI от +3.0%) ===")

    threading.Thread(target=handle_telegram_updates, daemon=True).start()
    threading.Thread(target=check_and_reset_at_midnight, daemon=True).start()

    symbols = fetch_perpetual_symbols()
    if not symbols:
        print("✗ Критическая ошибка: список символов пуст.")
        return

    for symbol in symbols:
        historical_data[symbol] = {'oi': [], 'price': []}

    print(f"✓ Мониторинг {len(symbols)} пар запущен.")

    while True:
        try:
            tickers = fetch_all_bybit_tickers()
            if not tickers:
                time.sleep(15)
                continue

            timestamp = int(datetime.now().timestamp())

            for ticker in tickers:
                symbol = ticker['symbol']
                if symbol not in historical_data:
                    continue

                try:
                    current_oi = float(ticker['openInterest'])
                    current_price = float(ticker['lastPrice'])
                except (ValueError, KeyError):
                    continue

                # Микропауза для снижения нагрузки на CPU
                time.sleep(0.01)

                # 1. Обновляем историю OI
                historical_data[symbol]['oi'].append({'value': current_oi, 'timestamp': timestamp})
                if len(historical_data[symbol]['oi']) > 30:
                    historical_data[symbol]['oi'] = [
                        x for x in historical_data[symbol]['oi'] if timestamp - x['timestamp'] <= TIME_WINDOW
                    ]

                # 2. Обновляем историю цены
                historical_data[symbol]['price'].append({'value': current_price, 'timestamp': timestamp})
                if len(historical_data[symbol]['price']) > 30:
                    historical_data[symbol]['price'] = [
                        x for x in historical_data[symbol]['price'] if timestamp - x['timestamp'] <= TIME_WINDOW
                    ]

                # 3. Проверка изменения данных за окно TIME_WINDOW
                if len(historical_data[symbol]['oi']) > 1 and len(historical_data[symbol]['price']) > 1:
                    old_oi = historical_data[symbol]['oi'][0]['value']
                    old_price = historical_data[symbol]['price'][0]['value']

                    oi_change = calculate_change(old_oi, current_oi)
                    price_change = calculate_change(old_price, current_price)

                    # Условие: Одновременный рост цены >= +2.5% И открытого интереса >= +3.0%
                    if price_change >= PRICE_INCREASE_THRESHOLD and oi_change >= OI_INCREASE_THRESHOLD:
                        last_time = last_alert_time.get(symbol, 0)
                        
                        # Проверяем, прошел ли кулдаун (10 минут)
                        if timestamp - last_time >= (COOLDOWN_MINUTES * 60):
                            msg = (
                                f"🚀 <b>{symbol}</b>: Набор позиций / Вливание в лонг\n\n"
                                f"📈 <b>Рост цены:</b> <code>+{price_change:.2f}%</code>\n"
                                f"📊 <b>Приток OI:</b> <code>+{oi_change:.2f}%</code>\n"
                                f"⏱ <b>Интервал:</b> последние 5 мин."
                            )
                            for chat_id in list(users.keys()):
                                send_telegram_notification(chat_id, msg, symbol)

                            # Обновляем время отправки алерта по символу
                            last_alert_time[symbol] = timestamp

            # Пауза между полными обходами рынка
            time.sleep(15)

        except Exception as e:
            print(f"✗ Ошибка основного цикла: {e}")
            time.sleep(15)


if __name__ == "__main__":
    main()
