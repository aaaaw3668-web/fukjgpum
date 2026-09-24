import json
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
import requests
import websocket

# ==================== НАСТРОЙКИ (ТОЛЬКО LONG) ====================
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
if not TELEGRAM_BOT_TOKEN:
    print("✗ Ошибка: TELEGRAM_BOT_TOKEN не найден в переменных окружения!")
    exit(1)

# --- Настройки для LONG (Рост цены + Рост ОИ) ---
LONG_PRICE_PUMP_THRESHOLD = 2.5   # Рост цены от Low за окно на 2.5% и более
LONG_MIN_OI_GROWTH_PCT = 3      # Рост ОИ от Low за окно на 2.5% и более

# --- Общие параметры ---
TIME_WINDOW = 60 * 5              # Окно анализа: 5 минут (300 сек)
COOLDOWN_MINUTES = 10            # Пауза между алертами по одной монете
DAILY_ALERT_LIMIT = 100          # Суточный лимит уведомлений на одну монету

# URL WebSocket Bybit (Linear Perpetuals)
BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"

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
last_alert_time = {}              # { 'BTCUSDT_LONG': timestamp }
symbol_24h_change = {}            # { 'BTCUSDT': float_изменения_за_24h }

# Блокировка для безопасной работы с потоками
data_lock = threading.Lock()


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
                            'text': f"✅ <b>Бот (ТОЛЬКО LONG с ростом ОИ) запущен!</b>\nЛимит: <b>{DAILY_ALERT_LIMIT} в сутки</b>.",
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

    while True:
        try:
            now = get_ye_time()
            if now >= reset_time:
                for chat_id in users:
                    users[chat_id]['alert_counts'] = {}

                reset_message = "🔄 <b>Сброс суточных лимитов завершен!</b>"
                broadcast_message(reset_message)

                reset_time = reset_time + timedelta(days=1)

            time.sleep(30)
        except Exception as e:
            print(f"✗ Ошибка сброса лимитов: {e}")
            time.sleep(30)


# ==================== ВСПОМОГАТЕЛЬНЫЙ REST ====================
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


# ==================== ОБРАБОТКА WEBSOCKET ДАННЫХ ====================
def process_ticker_update(symbol, data_payload):
    """Обработка ТОЛЬКО LONG сигналов (рост цены + рост ОИ)"""
    timestamp = int(datetime.now().timestamp())

    with data_lock:
        if symbol not in historical_data:
            historical_data[symbol] = {'price': [], 'oi': []}
            
        data = historical_data[symbol]

        if 'price24hPcnt' in data_payload:
            try:
                symbol_24h_change[symbol] = float(data_payload['price24hPcnt']) * 100
            except ValueError:
                pass

        if 'lastPrice' in data_payload:
            try:
                current_price = float(data_payload['lastPrice'])
                data['price'].append({'value': current_price, 'timestamp': timestamp})
            except ValueError:
                pass

        if 'openInterest' in data_payload:
            try:
                current_oi = float(data_payload['openInterest'])
                if current_oi > 0:
                    data['oi'].append({'value': current_oi, 'timestamp': timestamp})
            except ValueError:
                pass

        # Очистка устаревших данных (старше TIME_WINDOW)
        data['price'] = [x for x in data['price'] if timestamp - x['timestamp'] <= TIME_WINDOW]
        data['oi'] = [x for x in data['oi'] if timestamp - x['timestamp'] <= TIME_WINDOW]

        # ПРОВЕРКА УСЛОВИЙ
        if len(data['price']) > 1 and len(data['oi']) > 1:
            price_change_24h = symbol_24h_change.get(symbol, 0.0)
            current_price = data['price'][-1]['value']
            current_oi = data['oi'][-1]['value']

            min_price = min(x['value'] for x in data['price'])
            min_oi = min(x['value'] for x in data['oi'])

            # ---------------- ПРОВЕРКА LONG (Рост цены от Low + Рост ОИ от Low) ----------------
            long_price_pump = calculate_change(min_price, current_price)
            long_oi_growth = calculate_change(min_oi, current_oi)

            if long_price_pump >= LONG_PRICE_PUMP_THRESHOLD and long_oi_growth >= LONG_MIN_OI_GROWTH_PCT:
                long_key = f"{symbol}_LONG"
                last_time = last_alert_time.get(long_key, 0)

                if timestamp - last_time >= (COOLDOWN_MINUTES * 60):
                    msg = (
                        f"🚀 <b>{symbol}</b>: Памп / Набор ЛОНГА!\n\n"
                        f"📈 <b>Рост от Low (5м):</b> <code>+{long_price_pump:.2f}%</code>\n"
                        f"📈 <b>Рост ОИ от Low (5м):</b> <code>+{long_oi_growth:.2f}%</code>\n"
                        f"📊 <b>Тренд 24h:</b> <code>{price_change_24h:.2f}%</code>\n"
                        f"⏱ <b>Окно анализа:</b> 5 мин."
                    )
                    
                    threading.Thread(
                        target=send_to_all_users, 
                        args=(msg, symbol), 
                        daemon=True
                    ).start()

                    last_alert_time[long_key] = timestamp


def send_to_all_users(msg, symbol):
    for chat_id in list(users.keys()):
        send_telegram_notification(chat_id, msg, symbol)


# ==================== WEBSOCKET КЛИЕНТ ====================
def on_message(ws, message):
    try:
        data = json.loads(message)
        if "topic" in data and data["topic"].startswith("tickers."):
            symbol = data["topic"].split(".")[1]
            ticker_data = data.get("data", {})
            process_ticker_update(symbol, ticker_data)
    except Exception as e:
        print(f"✗ Ошибка обработки WS: {e}")


def on_error(ws, error):
    print(f"✗ WS Ошибка: {error}")


def on_close(ws, close_status_code, close_msg):
    print(f"⚠ WS Закрыто. Переподключение через 5 сек...")
    time.sleep(5)
    start_websocket_client(symbols_list)


def on_open(ws):
    print("✓ WS Соединение установлено. Отправка подписок...")
    batch_size = 10
    for i in range(0, len(symbols_list), batch_size):
        chunk = symbols_list[i:i + batch_size]
        topics = [f"tickers.{sym}" for sym in chunk]
        
        ws.send(json.dumps({"op": "subscribe", "args": topics}))
        time.sleep(0.05)
        
    print(f"✓ Подписались на {len(symbols_list)} тикеров!")


def start_websocket_client(symbols):
    global symbols_list
    symbols_list = symbols
    
    ws = websocket.WebSocketApp(
        BYBIT_WS_URL,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever(ping_interval=20, ping_timeout=10)


# ==================== MAIN ====================
def main():
    print("=== Запуск LONG мониторинга (Сигналы с ростом цены и ОИ) ===")

    threading.Thread(target=handle_telegram_updates, daemon=True).start()
    threading.Thread(target=check_and_reset_at_midnight, daemon=True).start()

    symbols = fetch_perpetual_symbols()
    if not symbols:
        print("✗ Ошибка: нет символов.")
        return

    start_websocket_client(symbols)


if __name__ == "__main__":
    main()
