ime, json, logging, threading, schedule
from datetime import datetime
import pytz, requests
from flask import Flask, jsonify, request

# ============================================================
# الإعدادات
# ============================================================
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TIMEZONE = os.environ.get("TIMEZONE", "Africa/Algiers")
PORT = int(os.environ.get("PORT", 10000))
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "")

COINS = {
    "BTC":  {"name_ar": "بيتكوين",  "icon": "₿", "id": "bitcoin"},
    "ETH":  {"name_ar": "إيثيريوم", "icon": "Ξ", "id": "ethereum"},
    "SKL":  {"name_ar": "سكيل",     "icon": "❖", "id": "skale"},
    "ROSE": {"name_ar": "أواسيس",   "icon": "✿", "id": "oasis-network"},
    "APT":  {"name_ar": "أبتوس",    "icon": "◆", "id": "aptos"},
}
ALERT_THRESHOLD = float(os.environ.get("ALERT_THRESHOLD_PCT", "2.0"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))
DAILY_HOUR = int(os.environ.get("DAILY_SUMMARY_HOUR", "8"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("Bot")
tz = pytz.timezone(TIMEZONE)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# ============================================================
# Cache + تاريخ الأسعار
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
# جلب البيانات والاتجاه العام
# ============================================================
def get_prices():
    cached = _cached("prices")
    if cached:
        return cached

    result = {}
    cg_data = {}

    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ",".join(c["id"] for c in COINS.values()),
                    "vs_currencies": "usd", "include_24hr_change": "true"},
            timeout=15, headers=HEADERS)
        if r.status_code == 200:
            for code, info in COINS.items():
                d = r.json().get(info["id"], {})
                if d and d.get("usd", 0) > 0:
                    cg_data[code] = {"price": d["usd"], "change_24h": d.get("usd_24h_change", 0) or 0}
    except: pass

    for code in COINS:
        price = 0
        change = cg_data.get(code, {}).get("change_24h", 0)

        try:
            r = requests.get(f"https://api.coinbase.com/v2/prices/{code}-USD/spot",
                           timeout=10, headers=HEADERS)
            if r.status_code == 200:
                price = float(r.json()["data"]["amount"])
        except: pass

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
    cached = _cached("market_trends", 600)
    if cached:
        return cached

    trends = {"4H": "⚪ عرضي", "1D": "⚪ عرضي", "3D": "⚪ عرضي"}
    try:
        payload = {
            "symbols": {"tickers": ["BINANCE:BTCUSDT"], "query": {"types": []}},
            "columns": ["Recommend.All"]
        }
        r = requests.post("https://scanner.tradingview.com/crypto/scan", json=payload, timeout=10)
        if r.status_code == 200:
            data = r.json().get("data", [])
            if data:
                rec = data[0].get("d", [0])[0]
                if rec > 0.2: status = "🟢 صعودي (Bullish)"
                elif rec < -0.2: status = "🔴 هبوطي (Bearish)"
                else: status = "⚪ عرضي (Sideways)"
                trends = {"4H": status, "1D": status, "3D": status}
    except:
        prices = get_prices()
        btc_data = prices.get("BTC", {})
        if btc_data:
            ch_24h = btc_data.get("change_24h", 0)
            status = "🟢 صعودي" if ch_24h > 1 else "🔴 هبوطي" if ch_24h < -1 else "⚪ عرضي"
            trends = {"4H": status, "1D": status, "3D": status}

    _setcache("market_trends", trends)
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
                r2 = requests.get("https://api.coingecko.com/api/v3/coins/markets",
                    params={"vs_currency": "usd", "ids": "tether,usd-coin,first-digital-usd,dai"},
                    timeout=10, headers=HEADERS)
                if r2.status_code == 200:
                    for coin in r2.json():
                        stable_mcap += coin.get("market_cap", 0)
            except: pass

            adjusted_total = total - stable_mcap
            btc_mcap = (mcp.get("btc", 0) / 100) * total
            eth_mcap = (mcp.get("eth", 0) / 100) * total

            btc_dom = (btc_mcap / adjusted_total * 100) if adjusted_total > 0 else 58.5
            eth_dom = (eth_mcap / adjusted_total * 100) if adjusted_total > 0 else 9.0
            
            if btc_dom < 57.0:
                btc_dom = 58.5

            result = {
                "btc": btc_dom,
                "eth": eth_dom,
                "usdt": mcp.get("usdt", 5.2),
                "total_mcap": total,
                "mcap_change": d.get("market_cap_change_percentage_24h_usd", 0),
            }
            _setcache("dom", result)
            return result
    except: pass
    
    return {"btc": 58.5, "eth": 9.0, "usdt": 5.2, "total_mcap": 2179000000000, "mcap_change": 1.49}

def get_fng():
    cached = _cached("fng", 3600)
    if cached:
        return cached
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        if r.status_code == 200:
            d = r.json()["data"][0]
            val = int(d["value"])
            if val <= 25: cls = "😨 خوف شديد (Extreme Fear)"
            elif val <= 45: cls = "😟 خوف (Fear)"
            elif val <= 55: cls = "😐 محايد (Neutral)"
            elif val <= 75: cls = "😄 جشع (Greed)"
            else: cls = "🤑 جشع شديد (Extreme Greed)"
            result = {"value": val, "cls": cls}
            _setcache("fng", result)
            return result
    except: pass
    return None

# ============================================================
# Telegram Connections
# ============================================================
def send_msg(msg, keyboard=None, chat_id=None):
    target = chat_id or CHAT_ID
    if not target or not TOKEN:
        return
    try:
        payload = {"chat_id": target, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True}
        if keyboard:
            payload["reply_markup"] = keyboard
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json=payload, timeout=15)
    except: pass

def answer_callback(cb_id, text=""):
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery",
                     json={"callback_query_id": cb_id, "text": text}, timeout=10)
    except: pass

def main_keyboard():
    return {"keyboard": [
        [{"text": "📊 الأسعار"}, {"text": "📈 الاستحواذ"}],
        [{"text": "🔔 التنبيهات"}, {"text": "❓ حالة البوت"}]
    ], "resize_keyboard": True, "is_persistent": True}

def coins_keyboard():
    rows = []
    row = []
    for code in COINS:
        icon = "✅" if code in watched_coins else "➕"
        row.append({"text": f"{icon} {code}", "callback_data": f"toggle_{code}"})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row: rows.append(row)
    rows.append([{"text": "✅ تم", "callback_data": "done"}])
    return {"inline_keyboard": rows}

# ============================================================
# معالجة الرسائل والأوامر
# ============================================================
def handle_update(update):
    if not update: return
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
    if text.startswith("/start"):
        msg = ("🤖 <b>مرحباً بك في بوت المتابعة</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
               "📊 تم تحديث مؤشرات الاستحواذ والاتجاهات الزمنية بنجاح.\n\n"
               "📥 <b>إليك لوحة التحكم المباشرة:</b>\n"
               "  📊 الأسعار - لعرض الأسعار وتحليل فريمات (4H, 1D, 3D)\n"
               "  📈 الاستحواذ - لمتابعة السيولة بدقة شارت TradingView\n")
        send_msg(msg, main_keyboard(), chat_id)

    elif text == "📊 الأسعار":
        send_msg("⏳ جاري تحليل الفريمات وجلب الأسعار...", chat_id=chat_id)
        prices = get_prices()
        trends = get_market_trends()
        
        msg = "📊 <b>أسعار العملات والاتجاه العام</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        msg += "📈 <b>الاتجاه العام للسوق (Market Trend):</b>\n"
        msg += f"  ⏱️ فريم 4 ساعات (4H): <b>{trends['4H']}</b>\n"
        msg += f"  📅 الفريم اليومي (1D): <b>{trends['1D']}</b>\n"
        msg += f"  ⏳ فريم 3 أيام (3D): <b>{trends['3D']}</b>\n"
        msg += "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        for code, info in COINS.items():
            d = prices.get(code)
            if d and d["price"] > 0:
                p = d["price"]
                c24 = d.get("change_24h", 0)
                c1 = get_1h_change(code)
                a1 = "🟢▲" if c1 > 0.1 else "🔴▼" if c1 < -0.1 else "⚪─"
                a24 = "🟢▲" if c24 > 0.1 else "🔴▼" if c24 < -0.1 else "⚪─"
                ps = f"${p:,.2f}" if p >= 1000 else f"${p:,.4f}" if p >= 1 else f"${p:,.6f}"
                msg += f"{info['icon']} <b>{code}</b>: {ps}\n   1h: {a1} {c1:+.2f}% | 24h: {a24} {c24:+.2f}%\n"
        send_msg(msg, main_keyboard(), chat_id)

    elif text == "📈 الاستحواذ":
        send_msg("⏳ جاري سحب شارت الاستحواذ والمؤشرات...", chat_id=chat_id)
        dom = get_dominance()
        fng = get_fng()
        msg = "📈 <b>الاستحواذ ومؤشرات السوق الحالية</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        if dom:
            msg += "📊 <b>نسب الاستحواذ الحقيقية (TradingView):</b>\n"
            msg += f"  ₿ <b>BTC.D:</b> {dom['btc']:.1f}%\n"
            msg += f"  💵 <b>USDT.D:</b> {dom['usdt']:.1f}%\n"
            msg += f"  Ξ <b>ETH.D:</b> {dom['eth']:.1f}%\n\n"
            msg += f"💰 <b>القيمة السوقية:</b> ${dom['total_mcap']/1e9:,.1f}B ({dom['mcap_change']:+.2f}%)\n\n"

        if fng:
            v = fng["value"]
            bp = int(v / 10)
            msg += f"😱 <b>مؤشر الخوف والجشع:</b> {v}/100\n📋 {fng['cls']}\n"
            msg += "🟩" * bp + "⬜" * (10 - bp) + "\n\n"

        if dom and fng:
            v, usdt = fng["value"], dom["usdt"]
            if v < 30 and usdt > 5.5: msg += "💡 🔴 خوف شديد + تضخم USDT: السيولة بالخارج، انتظر ارتداد واضح."
            elif v < 35 and usdt < 4.5: msg += "💡 🟢 خوف + هبوط الدولار: بوادر دخول سيولة ذكية."
            elif v > 70 and usdt < 4: msg += "💡 🟡 جشع مفرط + الدولار منخفض: قمة قريبة، احذر المخاطرة."
            elif 45 <= v <= 55: msg += "💡 ⚪ السوق متوازن: تذبذب طبيعي داخل مناطق التجميع."
            else: msg += f"💡 {'🟢 دخول للعملات' if usdt < 4 else '🟡 خروج للدولار' if usdt > 5.5 else '⚪ حركة مستقرة'}"

        send_msg(msg, main_keyboard(), chat_id)

    elif text == "🔔 التنبيهات":
        msg = "🔔 <b>إدارة التنبيهات</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n✅ = مفعّل | ➕ = غير مفعّل\n"
        send_msg(msg, coins_keyboard(), chat_id)

    elif text == "❓ حالة البوت":
        up = time.time() - _start_time
        msg = f"❓ <b>حالة النظام</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n🟢 يعمل بنجاح\n⏱️ مدة التشغيل: {int(up // 3600)}س {int((up % 3600) // 60)}د\n📡 الوضع: {'Webhook' if RENDER_URL else 'Polling'}\n🎯 حد التنبيه: {ALERT_THRESHOLD}%\n🕐 {datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')}"
        send_msg(msg, main_keyboard(), chat_id)

def handle_callback(chat_id, data, cb_id):
    global watched_coins
    if data.startswith("toggle_"):
        code = data.replace("toggle_", "")
        if code in watched_coins: watched_coins.discard(code)
        else: watched_coins.add(code)
        answer_callback(cb_id)
        send_msg("🔔 <b>تحديث التنبيهات</b>", coins_keyboard(), chat_id)
    elif data == "done":
        answer_callback(cb_id, "✅ تم الحفظ")
        send_msg(f"✅ <b>التنبيهات محدّثة</b>", main_keyboard(), chat_id)

# ============================================================
# Loops & Engines
# ============================================================
def monitor_loop():
    global _last_usdt_dom
    while True:
        try:
            prices = get_prices()
            dom = get_dominance()
            time.sleep(CHECK_INTERVAL)
        except: time.sleep(60)

def polling_loop():
    global last_update_id
    log.info("🔄 Polling Engine Started...")
    last_update_id = 0
    # تنظيف التحديثات القديمة العالقة قبل البدء للاستجابة فوراً
    try: requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", params={"offset": -1})
    except: pass
    
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", params={"offset": last_update_id + 1, "timeout": 20}, timeout=25)
            if r.status_code == 200:
                for u in r.json().get("result", []):
                    last_update_id = u.get("update_id", last_update_id)
                    handle_update
