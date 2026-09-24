import json
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
import requests

# ==================== НАСТРОЙКИ (ТОЛЬКО LONG) ====================
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
if not TELEGRAM_BOT_TOKEN:
    print("✗ Ошибка: TELEGRAM_BOT_TOKEN не найден в переменных окружения!")
    exit(1)

# --- Настройки условия сигнала ---
LONG_PRICE_PUMP_THRESHOLD = 1   # Рост цены от Low за 5 мин (в %)
LONG_MIN_OI_GROWTH_PCT = 5      # Рост ОИ от Low за 5 мин (в %)

# --- Параметры опроса API и контроля ---
TIME_WINDOW = 60 * 5              # Окно анализа: 5 минут (300 сек)
POLL_INTERVAL = 10                # Частота запросов к API Bybit (раз в 10 секунд)
COOLDOWN_MINUTES = 10             # Пауза между алертами по одной монете
DAILY_ALERT_LIMIT = 100           # Суточный лимит алертов на монету

# HTTP сессия с повторными попытками при сбоях сети
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
session.mount('https://', adapter)

# База пользователей в памяти
users = {
    '5296533274': {
        'active': True,
        'alert_counts': {}
    }
}

# Хранилище свеч/истории
historical_data = {}              # { 'BTCUSDT': { 'price': [...], 'oi': [...] } }
last_alert_time = {}              # { 'BTCUSDT': timestamp }
data_lock = threading.Lock()


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def get_ye_time():
    """Текущее время по Уфимскому часовому поясу (UTC+5)"""
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


# ==================== ТЕЛЕГРАМ БОТ ====================
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
        f"📊 <b>Алертов по {monospace_symbol} сегодня:</b> <code>{current_count}/{DAILY_ALERT_LIMIT}</code>"
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
        print(f"✓ LONG сигнал по {symbol} отправлен пользователю {chat_id}")
        return True
    except Exception as e:
        print(f"✗ Ошибка отправки в TG: {repr(e)}")
        return False


def handle_telegram_updates():
    """Простой Long Polling для команд /start и /stats"""
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
                        url_send = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                        payload = {
                            'chat_id': chat_id,
                            'text': f"✅ <b>Бот мониторинга LONG (Рост цены + Рост ОИ) запущен!</b>",
                            'parse_mode': 'HTML'
                        }
                        session.post(url_send, json=payload)

                    elif text == '/stats':
                        counts = users.get(chat_id, {}).get('alert_counts', {})
                        stats_text = f"📊 <b>Статистика LONG алертов за сегодня:</b>\n\n"
                        if counts:
                            for sym, count in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:20]:
                                stats_text += f"• <code>{sym}</code>: {count}/{DAILY_ALERT_LIMIT}\n"
                        else:
                            stats_text += "Сегодня алертов еще не было."

                        url_send = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                        session.post(url_send, json={'chat_id': chat_id, 'text': stats_text, 'parse_mode': 'HTML'})
            time.sleep(2)
        except Exception as e:
            time.sleep(5)


def check_and_reset_at_midnight():
    """Сброс суточных лимитов в 05:00 по Уфе"""
    now = get_ye_time()
    reset_time = now.replace(hour=5, minute=0, second=0, microsecond=0)
    if now >= reset_time:
        reset_time += timedelta(days=1)

    while True:
        try:
            now = get_ye_time()
            if now >= reset_time:
                for chat_id in users:
                    users[chat_id]['alert_counts'] = {}
                print("🔄 Суточные лимиты алертов сброшены.")
                reset_time += timedelta(days=1)
            time.sleep(30)
        except Exception:
            time.sleep(30)


# ==================== РАБОТА С REST API BYBIT ====================
def fetch_tickers_data():
    """Получает текущие цены, 24h изменение и Открытый Интерес по всем монетам через один REST-запрос"""
    url = "https://api.bybit.com/v5/market/tickers"
    params = {"category": "linear"}
    try:
        response = session.get(url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get('retCode') == 0:
                return data['result']['list']
    except Exception as e:
        print(f"✗ Ошибка обращения к Bybit REST API: {e}")
    return []


def process_market_data(tickers):
    """Анализирует поступившие данные рынка на соответствие LONG-условиям"""
    timestamp = int(datetime.now().timestamp())

    with data_lock:
        for ticker in tickers:
            symbol = ticker.get('symbol', '')
            if not symbol.endswith('USDT'):
                continue

            try:
                price = float(ticker.get('lastPrice', 0))
                oi = float(ticker.get('openInterest', 0))
                price_24h_change = float(ticker.get('price24hPcnt', 0)) * 100
            except (ValueError, TypeError):
                continue

            if price <= 0 or oi <= 0:
                continue

            if symbol not in historical_data:
                historical_data[symbol] = {'price': [], 'oi': []}

            data = historical_data[symbol]
            data['price'].append({'value': price, 'timestamp': timestamp})
            data['oi'].append({'value': oi, 'timestamp': timestamp})

            # Очищаем данные старее 5 минут (TIME_WINDOW)
            data['price'] = [x for x in data['price'] if timestamp - x['timestamp'] <= TIME_WINDOW]
            data['oi'] = [x for x in data['oi'] if timestamp - x['timestamp'] <= TIME_WINDOW]

            # Проверяем условие сигнала (нужно минимум 2 точки данных)
            if len(data['price']) > 1 and len(data['oi']) > 1:
                min_price = min(x['value'] for x in data['price'])
                min_oi = min(x['value'] for x in data['oi'])

                price_pump = calculate_change(min_price, price)
                oi_growth = calculate_change(min_oi, oi)

                # Главная логика: РОСТ ЦЕНЫ + РОСТ ОИ
                if price_pump >= LONG_PRICE_PUMP_THRESHOLD and oi_growth >= LONG_MIN_OI_GROWTH_PCT:
                    last_time = last_alert_time.get(symbol, 0)

                    # Проверка Кулдауна
                    if timestamp - last_time >= (COOLDOWN_MINUTES * 60):
                        msg = (
                            f"🚀 <b>{symbol}</b>: Памп / Набор ЛОНГА!\n\n"
                            f"📈 <b>Рост от Low (5м):</b> <code>+{price_pump:.2f}%</code>\n"
                            f"📈 <b>Рост ОИ от Low (5м):</b> <code>+{oi_growth:.2f}%</code>\n"
                            f"📊 <b>Тренд 24h:</b> <code>{price_24h_change:.2f}%</code>\n"
                            f"⏱ <b>Окно анализа:</b> 5 мин."
                        )

                        # Отправка уведомлений всем активным юзерам
                        for chat_id in list(users.keys()):
                            threading.Thread(
                                target=send_telegram_notification,
                                args=(chat_id, msg, symbol),
                                daemon=True
                            ).start()

                        last_alert_time[symbol] = timestamp


# ==================== MAIN LOOP ====================
def main():
    print("=== Запуск REST API Мониторинга LONG (Рост цены + Рост ОИ) ===")

    # Запускаем фоновые сервисы Telegram
    threading.Thread(target=handle_telegram_updates, daemon=True).start()
    threading.Thread(target=check_and_reset_at_midnight, daemon=True).start()

    # Главный цикл опроса Bybit REST API
    while True:
        start_time = time.time()
        
        tickers = fetch_tickers_data()
        if tickers:
            process_market_data(tickers)

        # Вычисляем время паузы до следующего опроса
        elapsed = time.time() - start_time
        sleep_time = max(1, POLL_INTERVAL - elapsed)
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
