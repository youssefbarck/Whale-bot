import os, time, json, logging, threading, schedule
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
# جلب الأسعار والبيانات
# ============================================================
def get_prices():
    cached = _cached("prices")
    if cached:
        return cached

    result = {}
    cg_data = {}

    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": ",".join(c["id"] for c in COINS.values()),
                "vs_currencies": "usd",
                "include_24hr_change": "true",
            },
            timeout=15,
            headers=HEADERS,
        )
        if r.status_code == 200:
            for code, info in COINS.items():
                d = r.json().get(info["id"], {})
                if d and d.get("usd", 0) > 0:
                    cg_data[code] = {
                        "price": d["usd"],
                        "change_24h": d.get("usd_24hr_change", 0) or 0,
                    }
    except Exception as e:
        log.warning(f"get_prices coingecko: {e}")

    for code in COINS:
        price = 0
        change = cg_data.get(code, {}).get("change_24h", 0)

        try:
            r = requests.get(
                f"https://api.coinbase.com/v2/prices/{code}-USD/spot",
                timeout=10,
                headers=HEADERS,
            )
            if r.status_code == 200:
                price = float(r.json()["data"]["amount"])
        except Exception as e:
            log.debug(f"get_prices coinbase {code}: {e}")

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
    """جلب اتجاه السوق من TradingView (4H, 1D, 3D)"""
    cached = _cached("trends", 600)
    if cached:
        return cached

    trends = {"4H": "⚪ عرضي", "1D": "⚪ عرضي", "3D": "⚪ عرضي"}
    timeframes = {"4H": "240", "1D": "1D", "3D": "3D"}

    for tf_name, tf_val in timeframes.items():
        try:
            payload = {
                "symbols": {"tickers": [f"BINANCE:BTCUSDT|{tf_val}"], "query": {"types": []}},
                "columns": ["Recommend.All"],
            }
            r = requests.post(
                "https://scanner.tradingview.com/crypto/scan",
                json=payload,
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                if data:
                    rec = data[0].get("d", [0])[0]
                    if rec > 0.2:
                        trends[tf_name] = "🟢 صعودي"
                    elif rec < -0.2:
                        trends[tf_name] = "🔴 هبوطي"
                    else:
                        trends[tf_name] = "⚪ عرضي"
        except Exception as e:
            log.debug(f"trend {tf_name}: {e}")

    # Fallback: استخدام تغير 24h إذا فشل TradingView
    if trends["1D"] == "⚪ عرضي":
        prices = get_prices()
        btc = prices.get("BTC", {})
        if btc:
            ch = btc.get("change_24h", 0)
            if ch > 1:
                status = "🟢 صعودي"
            elif ch < -1:
                status = "🔴 هبوطي"
            else:
                status = "⚪ عرضي"
            trends = {"4H": status, "1D": status, "3D": status}

    _setcache("trends", trends)
    return trends

def get_dominance():
    """استحواذ BTC + ETH بدون العملات المستقرة (مطابق TradingView)"""
    cached = _cached("dom", 1800)
    if cached:
        return cached

    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/global",
            timeout=15,
            headers=HEADERS,
        )
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
                    headers=HEADERS,
                )
                if r2.status_code == 200:
                    for coin in r2.json():
                        stable_mcap += coin.get("market_cap", 0)
            except Exception as e:
                log.debug(f"stable mcap: {e}")

            adjusted_total = total - stable_mcap
            btc_mcap = (mcp.get("btc", 0) / 100) * total
            eth_mcap = (mcp.get("eth", 0) / 100) * total

            btc_dom = (btc_mcap / adjusted_total * 100) if adjusted_total > 0 else mcp.get("btc", 0)
            eth_dom = (eth_mcap / adjusted_total * 100) if adjusted_total > 0 else mcp.get("eth", 0)

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
        log.warning(f"get_dominance: {e}")

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
                cls = "😨 خوف شديد (Extreme Fear)"
            elif val <= 45:
                cls = "😟 خوف (Fear)"
            elif val <= 55:
                cls = "😐 محايد (Neutral)"
            elif val <= 75:
                cls = "😄 جشع (Greed)"
            else:
                cls = "🤑 جشع شديد (Extreme Greed)"
            result = {"value": val, "cls": cls}
            _setcache("fng", result)
            return result
    except Exception as e:
        log.warning(f"get_fng: {e}")

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
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json=payload,
            timeout=15,
        )
    except Exception as e:
        log.warning(f"send_msg: {e}")

def answer_callback(cb_id, text=""):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery",
            json={"callback_query_id": cb_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"answer_callback: {e}")

def main_keyboard():
    return {
        "keyboard": [
            [{"text": "📊 الأسعار"}, {"text": "📈 الاستحواذ"}],
            [{"text": "🔔 التنبيهات"}, {"text": "❓ حالة البوت"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }

def coins_keyboard():
    rows = []
    row = []
    for code in COINS:
        icon = "✅" if code in watched_coins else "➕"
        row.append({"text": f"{icon} {code}", "callback_data": f"toggle_{code}"})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "✅ تم", "callback_data": "done"}])
    return {"inline_keyboard": rows}

# ============================================================
# معالجة الرسائل
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
            "🤖 <b>مرحباً بك في بوت المتابعة</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "📊 بوت متابعة العملات الذكي\n"
            f"👁️ تنبيه عند ≥ <b>{ALERT_THRESHOLD}%</b>\n\n"
            "📥 <b>لوحة التحكم:</b>\n"
            "  📊 الأسعار - عرض الأسعار + اتجاه (4H, 1D, 3D)\n"
            "  📈 الاستحواذ - BTC + USDT + ETH (مطابق TradingView)\n"
            "  🔔 التنبيهات - اختيار العملات\n"
            "  ❓ حالة البوت - معلومات\n"
        )
        send_msg(msg, main_keyboard(), chat_id)

    elif text == "📊 الأسعار":
        send_msg("⏳ جاري تحليل الفريمات وجلب الأسعار...", chat_id=chat_id)
        prices = get_prices()
        trends = get_market_trends()

        msg = "📊 <b>أسعار العملات والاتجاه العام</b>\n"
        msg += "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
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
                msg += f"{info['icon']} <b>{code}</b>: ❌\n"
        send_msg(msg, main_keyboard(), chat_id)

    elif text == "📈 الاستحواذ":
        send_msg("⏳ جاري جلب بيانات الاستحواذ...", chat_id=chat_id)
        dom = get_dominance()
        fng = get_fng()
        msg = "📈 <b>الاستحواذ ومؤشرات السوق</b>\n"
        msg += "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        if dom:
            msg += "📊 <b>نسب الاستحواذ (مطابق TradingView):</b>\n"
            msg += f"  ₿ <b>BTC.D:</b> {dom['btc']:.1f}%\n"
            msg += f"  💵 <b>USDT.D:</b> {dom['usdt']:.1f}%\n"
            msg += f"  Ξ <b>ETH.D:</b> {dom['eth']:.1f}%\n\n"

            msg += "💰 <b>السوق الكلي:</b>\n"
            msg += f"  💵 ${dom['total_mcap']/1e9:,.1f}B\n"
            ch = dom.get("mcap_change", 0)
            e = "🟢" if ch >= 0 else "🔴"
            msg += f"  {e} تغير 24h: {ch:+.2f}%\n\n"

            usdt = dom["usdt"]
            if usdt > 6:
                msg += "💵 <b>USDT مرتفع</b> - سيولة في الدولار (هبوطي)\n\n"
            elif usdt < 4:
                msg += "💵 <b>USDT منخفض</b> - سيولة في العملات (صعودي)\n\n"
            else:
                msg += "💵 <b>USDT متوازن</b>\n\n"

        if fng:
            v = fng["value"]
            bp = int(v / 10)
            msg += f"😱 <b>الخوف/الجشع:</b> {v}/100\n"
            msg += f"📋 {fng['cls']}\n"
            msg += "🟩" * bp + "⬜" * (10 - bp) + "\n\n"

        if dom and fng:
            v = fng["value"]
            usdt = dom["usdt"]
            if v < 30 and usdt > 5.5:
                msg += "💡 🔴 خوف شديد + تضخم USDT: السيولة بالخارج، انتظر ارتداد واضح."
            elif v < 35 and usdt < 4.5:
                msg += "💡 🟢 خوف + هبوط الدولار: بوادر دخول سيولة ذكية."
            elif v > 70 and usdt < 4:
                msg += "💡 🟡 جشع مفرط + الدولار منخفض: قمة قريبة، احذر المخاطرة."
            elif v > 70 and usdt > 5.5:
                msg += "💡 🔴 جشع + USDT مرتفع = توزيع"
            elif 45 <= v <= 55:
                msg += "💡 ⚪ السوق متوازن: تذبذب طبيعي"
            elif usdt > 5.5:
                msg += "💡 🟡 USDT يرتفع = خروج للدولار"
            elif usdt < 4:
                msg += "💡 🟢 USDT ينخفض = دخول للعملات"
            else:
                msg += "💡 ⚪ السوق متوازن"

        send_msg(msg, main_keyboard(), chat_id)

    elif text == "🔔 التنبيهات":
        msg = "🔔 <b>إدارة التنبيهات</b>\n"
        msg += "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        msg += "اختر العملات التي تريد تنبيهك عند تغيرها:\n\n"
        msg += "✅ = مفعّل | ➕ = غير مفعّل\n"
        send_msg(msg, coins_keyboard(), chat_id)

    elif text == "❓ حالة البوت":
        up = time.time() - _start_time
        h = int(up // 3600)
        m = int((up % 3600) // 60)
        watched = len(watched_coins)
        mode = "Webhook" if RENDER_URL else "Polling"
        now = datetime.now(tz)
        msg = (
            f"❓ <b>حالة البوت</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🟢 <b>يعمل</b>\n"
            f"⏱️ مدة التشغيل: {h}س {m}د\n"
            f"📡 الوضع: {mode}\n"
            f"👁️ عملات مراقبة: {watched}/{len(COINS)}\n"
            f"🎯 حد التنبيه: {ALERT_THRESHOLD}%\n"
            f"🔄 فحص كل: {CHECK_INTERVAL//60} دقيقة\n\n"
            f"🕐 {now.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        send_msg(msg, main_keyboard(), chat_id)

    else:
        send_msg("استخدم القائمة بالأسفل", main_keyboard(), chat_id)

def handle_callback(chat_id, data, cb_id):
    global watched_coins

    if data.startswith("toggle_"):
        code = data.replace("toggle_", "")
        if code in watched_coins:
            watched_coins.discard(code)
            answer_callback(cb_id, f"➖ تم إيقاف {code}")
        else:
            watched_coins.add(code)
            answer_callback(cb_id, f"➕ تم تفعيل {code}")

        msg = "🔔 <b>إدارة التنبيهات</b>\n"
        msg += "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        msg += "اختر العملات:\n✅ = مفعّل | ➕ = غير مفعّل\n"
        send_msg(msg, coins_keyboard(), chat_id)

    elif data == "done":
        answer_callback(cb_id, "✅ تم الحفظ")
        watched_list = ", ".join(sorted(watched_coins)) or "لا أحد"
        send_msg(
            f"✅ <b>التنبيهات محدّثة</b>\n👁️ مراقبة: {watched_list}",
            main_keyboard(),
            chat_id,
        )

# ============================================================
# مراقب الأسعار الذكي
# ============================================================
def monitor_loop():
    global _last_usdt_dom
    log.info(f"👁️ مراقب يعمل (حد {ALERT_THRESHOLD}%)")
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
            log.error(f"خطأ في المراقبة: {e}")
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
            last_alerts = {}

        now = time.time()
        if now - last_alerts.get(code, 0) < 3600:
            return

        last_alerts[code] = now
        try:
            with open("/tmp/last_alert.json", "w") as f:
                json.dump(last_alerts, f)
        except Exception:
            pass

        info = COINS[code]
        emoji = "🟢" if change_1h > 0 else "🔴"
        arrow = "📈" if change_1h > 0 else "📉"

        def fmt(p):
            if p >= 1000:
                return f"${p:,.2f}"
            elif p >= 1:
                return f"${p:,.4f}"
            elif p >= 0.01:
                return f"${p:,.6f}"
            else:
                return f"${p:,.8f}"

        msg = f"{emoji} <b>تنبيه - {info['name_ar']} ({code})</b>\n"
        msg += "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        msg += f"{arrow} <b>تغير 1h:</b> {change_1h:+.2f}%\n"
        msg += f"📊 <b>تغير 24h:</b> {change_24h:+.2f}%\n"
        msg += f"💲 <b>السعر:</b> {fmt(price)}\n\n"

        if change_1h > 0 and usdt_falling:
            msg += "💡 🟢🟢 <b>إشارة صعودية قوية</b>\n"
            msg += "  السعر يصعد + USDT ينخفض = سيولة تدخل\n"
        elif change_1h < 0 and usdt_rising:
            msg += "💡 🔴🔴 <b>إشارة هبوطية قوية</b>\n"
            msg += "  السعر يهبط + USDT يرتفع = سيولة تخرج\n"
        elif change_1h > 0 and usdt_rising:
            msg += "💡 🟡 <b>إشارة متضاربة</b>\n"
            msg += "  السعر يصعد لكن USDT ير
