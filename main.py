import os, time, json, logging, threading, schedule
from datetime import datetime
import pytz, requests
from flask import Flask, jsonify, request

# ============================================================
# ط§ظ„ط¥ط¹ط¯ط§ط¯ط§طھ
# ============================================================
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TIMEZONE = os.environ.get("TIMEZONE", "Africa/Algiers")
PORT = int(os.environ.get("PORT", 10000))
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "")

COINS = {
    "BTC":  {"name_ar": "ط¨ظٹطھظƒظˆظٹظ†",  "icon": "â‚؟", "id": "bitcoin"},
    "ETH":  {"name_ar": "ط¥ظٹط«ظٹط±ظٹظˆظ…", "icon": "خ‍", "id": "ethereum"},
    "SKL":  {"name_ar": "ط³ظƒظٹظ„",     "icon": "â‌–", "id": "skale"},
    "ROSE": {"name_ar": "ط£ظˆط§ط³ظٹط³",   "icon": "âœ؟", "id": "oasis-network"},
    "APT":  {"name_ar": "ط£ط¨طھظˆط³",    "icon": "â—†", "id": "aptos"},
}
ALERT_THRESHOLD = float(os.environ.get("ALERT_THRESHOLD_PCT", "2.0"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))
DAILY_HOUR = int(os.environ.get("DAILY_SUMMARY_HOUR", "8"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("Bot")
tz = pytz.timezone(TIMEZONE)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# ============================================================
# Cache + طھط§ط±ظٹط® ط§ظ„ط£ط³ط¹ط§ط±
# ============================================================
_cache = {}
price_history = {c: [] for c in COINS}
watched_coins = set(COINS.keys())
_last_usdt_dom = None
_start_time = time.time()

def _cached(key, ttl=300):
    if key in _cache and time.time() - _cache[key][0] < ttl:
        return _cache[key][1]
    return None

def _setcache(key, data):
    _cache[key] = (time.time(), data)

# ============================================================
# ط¬ظ„ط¨ ط§ظ„ط£ط³ط¹ط§ط± ظˆط§ظ„ط¨ظٹط§ظ†ط§طھ
# ============================================================
def get_prices():
    cached = _cached("prices")
    if cached:
        return cached

    result = {}
    cg_data = {}

    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": ",".join(c["id"] for c in COINS.values()),
                "vs_currencies": "usd",
                "include_24hr_change": "true"
            },
            timeout=15,
            headers=HEADERS
        )
        if r.status_code == 200:
            for code, info in COINS.items():
                d = r.json().get(info["id"], {})
                if d and d.get("usd", 0) > 0:
                    cg_data[code] = {
                        "price": d["usd"],
                        "change_24h": d.get("usd_24h_change", 0) or 0
                    }
    except Exception as e:
        log.warning(f"ط®ط·ط£ CoinGecko prices: {e}")

    for code in COINS:
        price = 0
        change = cg_data.get(code, {}).get("change_24h", 0)

        try:
            r = requests.get(
                f"https://api.coinbase.com/v2/prices/{code}-USD/spot",
                timeout=10,
                headers=HEADERS
            )
            if r.status_code == 200:
                price = float(r.json()["data"]["amount"])
        except Exception as e:
            log.warning(f"ط®ط·ط£ Coinbase {code}: {e}")

        if price == 0 and code in cg_data:
            price = cg_data[code]["price"]

        if price > 0:
            result[code] = {"price": price, "change_24h": change}
            price_history[code].append((time.time(), price))
            if len(price_history[code]) > 12:
                price_history[code] = price_history[code][-12:]

    if result:
        _setcache("prices", result)
    return result

def get_1h_change(coin):
    history = price_history.get(coin, [])
    if len(history) < 2:
        return 0
    old_price = history[0][1]
    new_price = history[-1][1]
    if old_price == 0:
        return 0
    return ((new_price - old_price) / old_price) * 100

def get_market_trends():
    cached = _cached("trends", 600)
    if cached:
        return cached

    trends = {"4H": "âڑھ ط¹ط±ط¶ظٹ", "1D": "âڑھ ط¹ط±ط¶ظٹ", "3D": "âڑھ ط¹ط±ط¶ظٹ"}
    timeframes = {"4H": "240", "1D": "1D", "3D": "3D"}

    for tf_name, tf_val in timeframes.items():
        try:
            payload = {
                "symbols": {"tickers": [f"BINANCE:BTCUSDT|{tf_val}"], "query": {"types": []}},
                "columns": ["Recommend.All"]
            }
            r = requests.post("https://scanner.tradingview.com/crypto/scan", json=payload, timeout=10)
            if r.status_code == 200:
                data = r.json().get("data", [])
                if data:
                    rec = data[0].get("d", [0])[0]
                    if rec > 0.2:
                        trends[tf_name] = "ًںں¢ طµط¹ظˆط¯ظٹ"
                    elif rec < -0.2:
                        trends[tf_name] = "ًں”´ ظ‡ط¨ظˆط·ظٹ"
                    else:
                        trends[tf_name] = "âڑھ ط¹ط±ط¶ظٹ"
        except Exception as e:
            log.warning(f"ط®ط·ط£ TradingView {tf_name}: {e}")

    if trends["1D"] == "âڑھ ط¹ط±ط¶ظٹ":
        prices = get_prices()
        btc = prices.get("BTC", {})
        if btc:
            ch = btc.get("change_24h", 0)
            if ch > 1:
                status = "ًںں¢ طµط¹ظˆط¯ظٹ"
            elif ch < -1:
                status = "ًں”´ ظ‡ط¨ظˆط·ظٹ"
            else:
                status = "âڑھ ط¹ط±ط¶ظٹ"
            trends = {"4H": status, "1D": status, "3D": status}

    _setcache("trends", trends)
    return trends

def get_dominance():
    cached = _cached("dom", 1800)
    if cached:
        return cached

    try:
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=15, headers=HEADERS)
        if r.status_code == 200:
            d = r.json()["data"]
            mcp = d.get("market_cap_percentage", {})
            total = d.get("total_market_cap", {}).get("usd", 0)

            stable_mcap = 0
            try:
                r2 = requests.get(
                    "https://api.coingecko.com/api/v3/coins/markets",
                    params={"vs_currency": "usd", "ids": "tether,usd-coin,first-digital-usd,dai"},
                    timeout=10,
                    headers=HEADERS
                )
                if r2.status_code == 200:
                    for coin in r2.json():
                        stable_mcap += coin.get("market_cap", 0)
            except Exception as e:
                log.warning(f"ط®ط·ط£ stable coins: {e}")

            adjusted_total = total - stable_mcap
            btc_mcap = (mcp.get("btc", 0) / 100) * total
            eth_mcap = (mcp.get("eth", 0) / 100) * total

            if adjusted_total > 0:
                btc_dom = (btc_mcap / adjusted_total) * 100
                eth_dom = (eth_mcap / adjusted_total) * 100
            else:
                btc_dom = mcp.get("btc", 0)
                eth_dom = mcp.get("eth", 0)

            result = {
                "btc": btc_dom,
                "eth": eth_dom,
                "usdt": mcp.get("usdt", 0),
                "total_mcap": total,
                "mcap_change": d.get("market_cap_change_percentage_24h_usd", 0),
            }
            _setcache("dom", result)
            return result
    except Exception as e:
        log.warning(f"ط®ط·ط£ dominance: {e}")

    return None

def get_fng():
    cached = _cached("fng", 3600)
    if cached:
        return cached

    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        if r.status_code == 200:
            d = r.json()["data"][0]
            val = int(d["value"])
            if val <= 25:
                cls = "ًںک¨ ط®ظˆظپ ط´ط¯ظٹط¯ (Extreme Fear)"
            elif val <= 45:
                cls = "ًںکں ط®ظˆظپ (Fear)"
            elif val <= 55:
                cls = "ًںکگ ظ…ط­ط§ظٹط¯ (Neutral)"
            elif val <= 75:
                cls = "ًںک„ ط¬ط´ط¹ (Greed)"
            else:
                cls = "ًں¤‘ ط¬ط´ط¹ ط´ط¯ظٹط¯ (Extreme Greed)"
            result = {"value": val, "cls": cls}
            _setcache("fng", result)
            return result
    except Exception as e:
        log.warning(f"ط®ط·ط£ Fear&Greed: {e}")

    return None

# ============================================================
# Telegram
# ============================================================
def send_msg(msg, keyboard=None, chat_id=None):
    target = chat_id or CHAT_ID
    if not target or not TOKEN:
        return
    try:
        payload = {
            "chat_id": target,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json=payload, timeout=15)
    except Exception as e:
        log.error(f"ط®ط·ط£ send_msg: {e}")

def answer_callback(cb_id, text=""):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery",
            json={"callback_query_id": cb_id, "text": text},
            timeout=10
        )
    except Exception as e:
        log.warning(f"ط®ط·ط£ answer_callback: {e}")

def main_keyboard():
    return {
        "keyboard": [
            [{"text": "ًں“ٹ ط§ظ„ط£ط³ط¹ط§ط±"}, {"text": "ًں“ˆ ط§ظ„ط§ط³طھط­ظˆط§ط°"}],
            [{"text": "ًں”” ط§ظ„طھظ†ط¨ظٹظ‡ط§طھ"}, {"text": "â‌“ ط­ط§ظ„ط© ط§ظ„ط¨ظˆطھ"}]
        ],
        "resize_keyboard": True,
        "is_persistent": True
    }

def coins_keyboard():
    rows = []
    row = []
    for code in COINS:
        icon = "âœ…" if code in watched_coins else "â‍•"
        row.append({"text": f"{icon} {code}", "callback_data": f"toggle_{code}"})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "âœ… طھظ…", "callback_data": "done"}])
    return {"inline_keyboard": rows}

# ============================================================
# ظ…ط¹ط§ظ„ط¬ط© ط§ظ„ط±ط³ط§ط¦ظ„
# ============================================================
def handle_update(update):
    msg = update.get("message", {})
    if msg:
        chat_id = msg.get("chat", {}).get("id")
        text = msg.get("text", "").strip()
        if chat_id and text:
            handle_message(chat_id, text)
        return

    cb = update.get("callback_query", {})
    if cb:
        cb_id = cb.get("id", "")
        data = cb.get("data", "")
        chat_id = cb.get("message", {}).get("chat", {}).get("id")
        if chat_id and data:
            handle_callback(chat_id, data, cb_id)

def handle_message(chat_id, text):
    if text == "/start":
        msg = (
            "ًں¤– <b>ظ…ط±ط­ط¨ط§ظ‹ ط¨ظƒ ظپظٹ ط¨ظˆطھ ط§ظ„ظ…طھط§ط¨ط¹ط©</b>\n"
            "â”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پ\n\n"
            "ًں“ٹ ط¨ظˆطھ ظ…طھط§ط¨ط¹ط© ط§ظ„ط¹ظ…ظ„ط§طھ ط§ظ„ط°ظƒظٹ\n"
            f"ًں‘پï¸ڈ طھظ†ط¨ظٹظ‡ ط¹ظ†ط¯ â‰¥ <b>{ALERT_THRESHOLD}%</b>\n\n"
            "ًں“¥ <b>ظ„ظˆط­ط© ط§ظ„طھط­ظƒظ…:</b>\n"
            "  ًں“ٹ ط§ظ„ط£ط³ط¹ط§ط± - ط¹ط±ط¶ ط§ظ„ط£ط³ط¹ط§ط± + ط§طھط¬ط§ظ‡ (4H, 1D, 3D)\n"
            "  ًں“ˆ ط§ظ„ط§ط³طھط­ظˆط§ط° - BTC + USDT + ETH (ظ…ط·ط§ط¨ظ‚ TradingView)\n"
            "  ًں”” ط§ظ„طھظ†ط¨ظٹظ‡ط§طھ - ط§ط®طھظٹط§ط± ط§ظ„ط¹ظ…ظ„ط§طھ\n"
            "  â‌“ ط­ط§ظ„ط© ط§ظ„ط¨ظˆطھ - ظ…ط¹ظ„ظˆظ…ط§طھ\n"
        )
        send_msg(msg, main_keyboard(), chat_id)

    elif text == "ًں“ٹ ط§ظ„ط£ط³ط¹ط§ط±":
        send_msg("âڈ³ ط¬ط§ط±ظٹ طھط­ظ„ظٹظ„ ط§ظ„ظپط±ظٹظ…ط§طھ ظˆط¬ظ„ط¨ ط§ظ„ط£ط³ط¹ط§ط±...", chat_id=chat_id)
        prices = get_prices()
        trends = get_market_trends()

        msg = "ًں“ٹ <b>ط£ط³ط¹ط§ط± ط§ظ„ط¹ظ…ظ„ط§طھ ظˆط§ظ„ط§طھط¬ط§ظ‡ ط§ظ„ط¹ط§ظ…</b>\n"
        msg += "â”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پ\n\n"
        msg += "ًں“ˆ <b>ط§ظ„ط§طھط¬ط§ظ‡ ط§ظ„ط¹ط§ظ… ظ„ظ„ط³ظˆظ‚ (Market Trend):</b>\n"
        msg += f"  âڈ±ï¸ڈ ظپط±ظٹظ… 4 ط³ط§ط¹ط§طھ (4H): <b>{trends['4H']}</b>\n"
        msg += f"  ًں“… ط§ظ„ظپط±ظٹظ… ط§ظ„ظٹظˆظ…ظٹ (1D): <b>{trends['1D']}</b>\n"
        msg += f"  âڈ³ ظپط±ظٹظ… 3 ط£ظٹط§ظ… (3D): <b>{trends['3D']}</b>\n"
        msg += "â”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پ\n\n"

        for code, info in COINS.items():
            d = prices.get(code)
            if d and d["price"] > 0:
                p = d["price"]
                c24 = d.get("change_24h", 0)
                c1 = get_1h_change(code)
                a1 = "ًںں¢â–²" if c1 > 0.1 else "ًں”´â–¼" if c1 < -0.1 else "âڑھâ”€"
                a24 = "ًںں¢â–²" if c24 > 0.1 else "ًں”´â–¼" if c24 < -0.1 else "âڑھâ”€"
                if p >= 1000:
                    ps = f"${p:,.2f}"
                elif p >= 1:
                    ps = f"${p:,.4f}"
                elif p >= 0.01:
                    ps = f"${p:,.6f}"
                else:
                    ps = f"${p:,.8f}"
                msg += f"{info['icon']} <b>{code}</b>: {ps}\n"
                msg += f"   1h: {a1} {c1:+.2f}% | 24h: {a24} {c24:+.2f}%\n"
            else:
                msg += f"{info['icon']} <b>{code}</b>: â‌Œ\n"
        send_msg(msg, main_keyboard(), chat_id)

    elif text == "ًں“ˆ ط§ظ„ط§ط³طھط­ظˆط§ط°":
        send_msg("âڈ³ ط¬ط§ط±ظٹ ط¬ظ„ط¨ ط¨ظٹط§ظ†ط§طھ ط§ظ„ط§ط³طھط­ظˆط§ط°...", chat_id=chat_id)
        dom = get_dominance()
        fng = get_fng()
        msg = "ًں“ˆ <b>ط§ظ„ط§ط³طھط­ظˆط§ط° ظˆظ…ط¤ط´ط±ط§طھ ط§ظ„ط³ظˆظ‚</b>\n"
        msg += "â”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پ\n\n"

        if dom:
            msg += "ًں“ٹ <b>ظ†ط³ط¨ ط§ظ„ط§ط³طھط­ظˆط§ط° (ظ…ط·ط§ط¨ظ‚ TradingView):</b>\n"
            msg += f"  â‚؟ <b>BTC.D:</b> {dom['btc']:.1f}%\n"
            msg += f"  ًں’µ <b>USDT.D:</b> {dom['usdt']:.1f}%\n"
            msg += f"  خ‍ <b>ETH.D:</b> {dom['eth']:.1f}%\n\n"
            msg += "ًں’° <b>ط§ظ„ط³ظˆظ‚ ط§ظ„ظƒظ„ظٹ:</b>\n"
            msg += f"  ًں’µ ${dom['total_mcap']/1e9:,.1f}B\n"
            ch = dom.get("mcap_change", 0)
            e = "ًںں¢" if ch >= 0 else "ًں”´"
            msg += f"  {e} طھط؛ظٹط± 24h: {ch:+.2f}%\n\n"

            usdt = dom["usdt"]
            if usdt > 6:
                msg += "ًں’µ <b>USDT ظ…ط±طھظپط¹</b> - ط³ظٹظˆظ„ط© ظپظٹ ط§ظ„ط¯ظˆظ„ط§ط± (ظ‡ط¨ظˆط·ظٹ)\n\n"
            elif usdt < 4:
                msg += "ًں’µ <b>USDT ظ…ظ†ط®ظپط¶</b> - ط³ظٹظˆظ„ط© ظپظٹ ط§ظ„ط¹ظ…ظ„ط§طھ (طµط¹ظˆط¯ظٹ)\n\n"
            else:
                msg += "ًں’µ <b>USDT ظ…طھظˆط§ط²ظ†</b>\n\n"

        if fng:
            v = fng["value"]
            bp = int(v / 10)
            msg += f"ًںک± <b>ط§ظ„ط®ظˆظپ/ط§ظ„ط¬ط´ط¹:</b> {v}/100\n"
            msg += f"ًں“‹ {fng['cls']}\n"
            msg += "ًںں©" * bp + "â¬œ" * (10 - bp) + "\n\n"

        if dom and fng:
            v_val = fng["value"]
            usdt_val = dom["usdt"]
            if v_val < 30 and usdt_val > 5.5:
                msg += "ًں’، ًں”´ ط®ظˆظپ ط´ط¯ظٹط¯ + طھط¶ط®ظ… USDT: ط§ظ„ط³ظٹظˆظ„ط© ط¨ط§ظ„ط®ط§ط±ط¬طŒ ط§ظ†طھط¸ط± ط§ط±طھط¯ط§ط¯ ظˆط§ط¶ط­."
            elif v_val < 35 and usdt_val < 4.5:
                msg += "ًں’، ًںں¢ ط®ظˆظپ + ظ‡ط¨ظˆط· ط§ظ„ط¯ظˆظ„ط§ط±: ط¨ظˆط§ط¯ط± ط¯ط®ظˆظ„ ط³ظٹظˆظ„ط© ط°ظƒظٹط©."
            elif v_val > 70 and usdt_val < 4:
                msg += "ًں’، ًںں، ط¬ط´ط¹ ظ…ظپط±ط· + ط§ظ„ط¯ظˆظ„ط§ط± ظ…ظ†ط®ظپط¶: ظ‚ظ…ط© ظ‚ط±ظٹط¨ط©طŒ ط§ط­ط°ط± ط§ظ„ظ…ط®ط§ط·ط±ط©."
            elif v_val > 70 and usdt_val > 5.5:
                msg += "ًں’، ًں”´ ط¬ط´ط¹ + USDT ظ…ط±طھظپط¹ = طھظˆط²ظٹط¹"
            elif 45 <= v_val <= 55:
                msg += "ًں’، âڑھ ط§ظ„ط³ظˆظ‚ ظ…طھظˆط§ط²ظ†: طھط°ط¨ط°ط¨ ط·ط¨ظٹط¹ظٹ"
            elif usdt_val > 5.5:
                msg += "ًں’، ًںں، USDT ظٹط±طھظپط¹ = ط®ط±ظˆط¬ ظ„ظ„ط¯ظˆظ„ط§ط±"
            elif usdt_val < 4:
                msg += "ًں’، ًںں¢ USDT ظٹظ†ط®ظپط¶ = ط¯ط®ظˆظ„ ظ„ظ„ط¹ظ…ظ„ط§طھ"
            else:
                msg += "ًں’، âڑھ ط§ظ„ط³ظˆظ‚ ظ…طھظˆط§ط²ظ†"

        send_msg(msg, main_keyboard(), chat_id)

    elif text == "ًں”” ط§ظ„طھظ†ط¨ظٹظ‡ط§طھ":
        msg = "ًں”” <b>ط¥ط¯ط§ط±ط© ط§ظ„طھظ†ط¨ظٹظ‡ط§طھ</b>\n"
        msg += "â”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پ\n\n"
        msg += "ط§ط®طھط± ط§ظ„ط¹ظ…ظ„ط§طھ ط§ظ„طھظٹ طھط±ظٹط¯ طھظ†ط¨ظٹظ‡ظƒ ط¹ظ†ط¯ طھط؛ظٹط±ظ‡ط§:\n\n"
        msg += "âœ… = ظ…ظپط¹ظ‘ظ„ | â‍• = ط؛ظٹط± ظ…ظپط¹ظ‘ظ„\n"
        send_msg(msg, coins_keyboard(), chat_id)

    elif text == "â‌“ ط­ط§ظ„ط© ط§ظ„ط¨ظˆطھ":
        up = time.time() - _start_time
        h = int(up // 3600)
        m = int((up % 3600) // 60)
        watched = len(watched_coins)
        mode = "Webhook" if RENDER_URL else "Polling"
        now = datetime.now(tz)
        msg = (
            f"â‌“ <b>ط­ط§ظ„ط© ط§ظ„ط¨ظˆطھ</b>\n"
            f"â”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پ\n\n"
            f"ًںں¢ <b>ظٹط¹ظ…ظ„</b>\n"
            f"âڈ±ï¸ڈ ظ…ط¯ط© ط§ظ„طھط´ط؛ظٹظ„: {h}ط³ {m}ط¯\n"
            f"ًں“، ط§ظ„ظˆط¶ط¹: {mode}\n"
            f"ًں‘پï¸ڈ ط¹ظ…ظ„ط§طھ ظ…ط±ط§ظ‚ط¨ط©: {watched}/{len(COINS)}\n"
            f"ًںژ¯ ط­ط¯ ط§ظ„طھظ†ط¨ظٹظ‡: {ALERT_THRESHOLD}%\n"
            f"ًں”„ ظپط­طµ ظƒظ„: {CHECK_INTERVAL//60} ط¯ظ‚ظٹظ‚ط©\n\n"
            f"ًں•گ {now.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        send_msg(msg, main_keyboard(), chat_id)

    else:
        send_msg("ط§ط³طھط®ط¯ظ… ط§ظ„ظ‚ط§ط¦ظ…ط© ط¨ط§ظ„ط£ط³ظپظ„", main_keyboard(), chat_id)

def handle_callback(chat_id, data, cb_id):
    global watched_coins

    if data.startswith("toggle_"):
        code = data.replace("toggle_", "")
        if code in watched_coins:
            watched_coins.discard(code)
            answer_callback(cb_id, f"â‍– طھظ… ط¥ظٹظ‚ط§ظپ {code}")
        else:
            watched_coins.add(code)
            answer_callback(cb_id, f"â‍• طھظ… طھظپط¹ظٹظ„ {code}")

        msg = "ًں”” <b>ط¥ط¯ط§ط±ط© ط§ظ„طھظ†ط¨ظٹظ‡ط§طھ</b>\n"
        msg += "â”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پ\n\n"
        msg += "ط§ط®طھط± ط§ظ„ط¹ظ…ظ„ط§طھ:\nâœ… = ظ…ظپط¹ظ‘ظ„ | â‍• = ط؛ظٹط± ظ…ظپط¹ظ‘ظ„\n"
        send_msg(msg, coins_keyboard(), chat_id)

    elif data == "done":
        answer_callback(cb_id, "âœ… طھظ… ط§ظ„ط­ظپط¸")
        watched_list = ", ".join(sorted(watched_coins)) or "ظ„ط§ ط£ط­ط¯"
        send_msg(
            f"âœ… <b>ط§ظ„طھظ†ط¨ظٹظ‡ط§طھ ظ…ط­ط¯ظ‘ط«ط©</b>\nًں‘پï¸ڈ ظ…ط±ط§ظ‚ط¨ط©: {watched_list}",
            main_keyboard(),
            chat_id
        )

# ============================================================
# ظ…ط±ط§ظ‚ط¨ ط§ظ„ط£ط³ط¹ط§ط± ط§ظ„ط°ظƒظٹ
# ============================================================
def monitor_loop():
    global _last_usdt_dom
    log.info(f"ًں‘پï¸ڈ ظ…ط±ط§ظ‚ط¨ ظٹط¹ظ…ظ„ (ط­ط¯ {ALERT_THRESHOLD}%)")
    time.sleep(10)

    while True:
        try:
            prices = get_prices()
            dom = get_dominance()

            usdt_rising = False
            usdt_falling = False
            if dom and _last_usdt_dom is not None:
                usdt_diff = dom["usdt"] - _last_usdt_dom
                if usdt_diff > 0.1:
                    usdt_rising = True
                elif usdt_diff < -0.1:
                    usdt_falling = True
            if dom:
                _last_usdt_dom = dom["usdt"]

            for code in watched_coins:
                if code not in prices:
                    continue
                check_smart_alert(code, prices, dom, usdt_rising, usdt_falling)

            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            log.error(f"ط®ط·ط£ ظپظٹ ط§ظ„ظ…ط±ط§ظ‚ط¨ط©: {e}")
            time.sleep(60)

def check_smart_alert(code, prices, dom, usdt_rising, usdt_falling):
    data = prices.get(code)
    if not data or data["price"] == 0:
        return

    price = data["price"]
    change_24h = data.get("change_24h", 0)
    change_1h = get_1h_change(code)

    if abs(change_1h) >= ALERT_THRESHOLD:
        last_alerts = {}
        try:
            with open("/tmp/last_alert.json", "r") as f:
                last_alerts = json.load(f)
        except Exception:
            pass

        now
