# --- ОБНОВЛЕННЫЕ ПАРАМЕТРЫ ФИЛЬТРАЦИИ ---
TIMEFRAME = "15"
LOOKBACK_CANDLES = 60      # Смотрим уровень за последние 15 часов (60 свечей), а не 5
MIN_SWEEP_PCT = 1.0        # Минимальный вынос за уровень: от 1.0% (отсекает микро-шум 0.2-0.3%)
MAX_SWEEP_PCT = 5.0        # Если вынос больше 5% — это бешеный памп/слив, в ложный пробой не лезем
VOLUME_MULTIPLIER = 1.8    # Объем должен быть почти в 2 раза выше среднего (1.8x)
MIN_TURNOVER_24H = 15_000_000  # Поднимаем планку объема до $15M

def check_false_breakout(symbol: str):
    candles = get_klines(symbol, limit=LOOKBACK_CANDLES + 2)
    if len(candles) < LOOKBACK_CANDLES:
        return

    trigger_candle = candles[-1]
    history = candles[:-1]

    # Локальные хай и лоу за 15 часов
    range_high = max(c["high"] for c in history)
    range_low = min(c["low"] for c in history)

    avg_vol = sum(c["volume"] for c in history) / len(history)

    c_open = trigger_candle["open"]
    c_close = trigger_candle["close"]
    c_high = trigger_candle["high"]
    c_low = trigger_candle["low"]
    c_vol = trigger_candle["volume"]
    c_time = trigger_candle["start"]

    candle_range = c_high - c_low
    if candle_range == 0:
        return

    # --- 1. МЕДВЕЖИЙ ЛОЖНЫЙ ПРОБОЙ (SHORT) ---
    if c_high > range_high and c_close < range_high:
        sweep_pct = ((c_high - range_high) / range_high) * 100
        upper_wick = c_high - max(c_open, c_close)
        wick_ratio = upper_wick / candle_range

        # Фильтры:
        # 1. Вынос от 1% до 5%
        # 2. Тень сверху >= 50% всей свечи
        # 3. Закрытие строго КРАСНОЙ свечой (давление продавца)
        # 4. Всплеск объема от 1.8x
        if (MIN_SWEEP_PCT <= sweep_pct <= MAX_SWEEP_PCT and 
            wick_ratio >= 0.50 and 
            c_close < c_open and 
            c_vol >= avg_vol * VOLUME_MULTIPLIER):

            event_key = (symbol, c_time, "BEARISH")
            if event_key not in notified_events:
                notified_events.add(event_key)
                send_tg(
                    f"🪤 <b>ЛОЖНЫЙ ВЫНОС ХАЯ (SHORT): {symbol}</b>\n\n"
                    f"• Уровень (High за 15ч): <code>{range_high}</code>\n"
                    f"• Вынос тенью: <code>+{sweep_pct:.2f}%</code> (High: <code>{c_high}</code>)\n"
                    f"• Закрытие: <code>{c_close}</code> (красная свеча)\n"
                    f"• Доля верхней тени: <code>{wick_ratio * 100:.0f}%</code>\n"
                    f"• Всплеск объёма: <code>{c_vol/avg_vol:.1f}x</code>\n\n"
                    f"🔗 <a href='https://www.bybit.com/trade/usdt/{symbol}'>Bybit</a> | "
                    f"<a href='https://www.coinglass.com/tv/Bybit_{symbol}'>CoinGlass</a>"
                )

    # --- 2. БЫЧИЙ ЛОЖНЫЙ ПРОБОЙ (LONG) ---
    elif c_low < range_low and c_close > range_low:
        sweep_pct = ((range_low - c_low) / range_low) * 100
        lower_wick = min(c_open, c_close) - c_low
        wick_ratio = lower_wick / candle_range

        # Фильтры:
        # 1. Вынос от 1% до 5%
        # 2. Тень снизу >= 50% всей свечи
        # 3. Закрытие строго ЗЕЛЕНОЙ свечой (откуп)
        # 4. Всплеск объема от 1.8x
        if (MIN_SWEEP_PCT <= sweep_pct <= MAX_SWEEP_PCT and 
            wick_ratio >= 0.50 and 
            c_close > c_open and 
            c_vol >= avg_vol * VOLUME_MULTIPLIER):

            event_key = (symbol, c_time, "BULLISH")
            if event_key not in notified_events:
                notified_events.add(event_key)
                send_tg(
                    f"🚀 <b>ЛОЖНЫЙ ВЫНОС ЛОЯ (LONG): {symbol}</b>\n\n"
                    f"• Уровень (Low за 15ч): <code>{range_low}</code>\n"
                    f"• Вынос тенью: <code>-{sweep_pct:.2f}%</code> (Low: <code>{c_low}</code>)\n"
                    f"• Закрытие: <code>{c_close}</code> (зеленая свеча)\n"
                    f"• Доля нижней тени: <code>{wick_ratio * 100:.0f}%</code>\n"
                    f"• Всплеск объёма: <code>{c_vol/avg_vol:.1f}x</code>\n\n"
                    f"🔗 <a href='https://www.bybit.com/trade/usdt/{symbol}'>Bybit</a> | "
                    f"<a href='https://www.coinglass.com/tv/Bybit_{symbol}'>CoinGlass</a>"
                )
