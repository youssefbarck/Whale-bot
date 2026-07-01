import os
import time
import json
import logging
import threading
import schedule
from datetime import datetime
import pytz
import requests
from flask import Flask, jsonify, request

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
ALERT_THRESHOLD = 2.0
CHECK_INTERVAL = 300
DAILY_HOUR = 8

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("Bot")
tz = pytz.timezone(TIMEZONE)
HEADERS = {"User-Agent": "Mozilla/5.0"}

_cache = {}
price_history = {}
watched_coins = set()
_last_usdt_dom = None
_start_time = time.time()
_user_state = {}
CUSTOM_FILE = "/tmp/custom_coins.json"
_started = False

def load_custom():
    global COINS, price_history, watched_coins
    try:
        with open(CUSTOM_FILE, "r") as f:
            custom = json.load(f)
        for code, info in custom.items():
            if code not in COINS:
                COINS[code] = info
                watched_coins.add(code)
                price_history[code] = []
    except:
        pass
    for code in COINS:
        if code not in price_history:
            price_history[code] = []
        watched_coins.add(code)

def save_custom():
    try:
        custom = {k: v for k, v in COINS.items() if v.get("custom")}
        with open(CUSTOM_FILE, "w") as f:
            json.dump(custom, f, ensure_ascii=False)
    except:
        pass

def get_cached(key, ttl=120):
    if key in _cache and time.time() - _cache[key][0] < ttl:
        return _cache[key][1]
    return None

def set_cached(key, data):
    _cache[key] = (time.time(), data)

def get_prices():
    cached = get_cached("prices", 120)
    if cached:
        return cached
    result = {}
    try:
        ids = ",".join(c["id"] for c in COINS.values())
        r = requests.get("https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": "usd", "ids": ids, "sparkline": "false", "price_change_percentage": "24h"},
            timeout=15, headers=HEADERS)
        if r.status_code == 200:
            for coin in r.json():
                for code, info in COINS.items():
                    if info["id"] == coin.get("id"):
                        result[code] = {"price": coin.get("current_price", 0), "change_24h": coin.get("price_change_percentage_24h", 0) or 0}
                        break
    except Exception as e:
        log.warning(f"CG err: {e}")
    for code in COINS:
        if code not in result or result[code]["price"] == 0:
            try:
                r = requests.get(f"https://api.coinbase.com/v2/prices/{code}-USD/spot", timeout=10, headers=HEADERS)
                if r.status_code == 200:
                    p = float(r.json()["data"]["amount"])
                    if code not in result:
                        result[code] = {"price": p, "change_24h": 0}
                    elif result[code]["price"] == 0:
                        result[code]["price"] = p
            except:
                pass
    if result:
        for code in result:
            if code not in price_history:
                price_history[code] = []
            price_history[code].append((time.time(), result[code]["price"]))
            if len(price_history[code]) > 12:
                price_history[code] = price_history[code][-12:]
        set_cached("prices", result)
    return result

def get_1h_change(coin):
    h = price_history.get(coin, [])
    if len(h) < 2:
        return 0
    o, n = h[0][1], h[-1][1]
    return ((n - o) / o) * 100 if o != 0 else 0

def get_dominance():
    cached = get_cached("dom", 1800)
    if cached:
        return cached
    try:
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=15, headers=HEADERS)
        if r.status_code == 200:
            d = r.json()["data"]
            mcp = d.get("market_cap_percentage", {})
            res = {"btc": mcp.get("btc", 0), "eth": mcp.get("eth", 0), "usdt": mcp.get("usdt", 0),
                   "total_mcap": d.get("total_market_cap", {}).get("usd", 0),
                   "mcap_change": d.get("market_cap_change_percentage_24h_usd", 0)}
            set_cached("dom", res)
            return res
    except:
        pass
    return None

def get_fng():
    cached = get_cached("fng", 3600)
    if cached:
        return cached
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        if r.status_code == 200:
            d = r.json()["data"][0]
            v = int(d["value"])
            cls = "😨 خوف شديد" if v<=25 else "😟 خوف" if v<=45 else "😐 محايد" if v<=55 else "😄 جشع" if v<=75 else "🤑 جشع شديد"
            res = {"value": v, "cls": cls}
            set_cached("fng", res)
            return res
    except:
        pass
    return None

def search_coin(symbol):
    try:
        r = requests.get("https://api.coingecko.com/api/v3/search", params={"query": symbol}, timeout=10, headers=HEADERS)
        if r.status_code == 200:
            coins = r.json().get("coins", [])
            for coin in coins:
                if coin.get("symbol", "").upper() == symbol.upper():
                    return coin.get("id"), coin.get("name", symbol)
            if coins:
                return coins[0].get("id"), coins[0].get("name", symbol)
    except:
        pass
    return None, None

def send_msg(msg, kb=None, cid=None):
    t = cid or CHAT_ID
    if not t or not TOKEN:
        return
    try:
        p = {"chat_id": t, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True}
        if kb:
            p["reply_markup"] = kb
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json=p, timeout=15)
    except:
        pass

def main_kb():
    return {"keyboard": [
        [{"text": "📊 الأسعار"}, {"text": "📈 الاستحواذ"}],
        [{"text": "🔔 التنبيهات"}, {"text": "➕ إضافة عملة"}, {"text": "❓ حالة"}]
    ], "resize_keyboard": True, "is_persistent": True}

def coins_kb():
    rows, row = [], []
    for c in sorted(COINS.keys()):
        i = "✅" if c in watched_coins else "➕"
        row.append({"text": f"{i} {c}", "callback_data": f"toggle_{c}"})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "✅ تم", "callback_data": "done"}])
    return {"inline_keyboard": rows}

def handle_update(u):
    m = u.get("message", {})
    if m:
        cid = m.get("chat", {}).get("id")
        txt = m.get("text", "").strip()
        if cid and txt:
            handle_msg(cid, txt)
        return
    cb = u.get("callback_query", {})
    if cb:
        cid = cb.get("message", {}).get("chat", {}).get("id")
        d = cb.get("data", "")
        cb_id = cb.get("id", "")
        if cid and d:
            handle_cb(cid, d, cb_id)

def handle_msg(cid, txt):
    if _user_state.get(cid) == "waiting_for_coin":
        handle_coin_input(cid, txt)
        return
    if txt == "/start":
        msg = "🤖 <b>مرحباً</b>\n📊 بوت متابعة العملات\n"
        msg += f"👁️ تنبيه عند ≥ {ALERT_THRESHOLD}%\n"
        msg += f"🎯 عملاتك: {', '.join(sorted(COINS.keys()))}"
        send_msg(msg, main_kb(), cid)
    elif txt == "📊 الأسعار":
        send_msg("⏳ جاري الجلب...", cid=cid)
        pr = get_prices()
        msg = "📊 <b>الأسعار</b>\n━━━━━━━━━━━━━\n"
        for c in sorted(COINS.keys()):
            i = COINS[c]
            d = pr.get(c)
            if d and d["price"] > 0:
                p = d["price"]; c24 = d.get("change_24h", 0); c1 = get_1h_change(c)
                a1 = "🟢" if c1 > 0.1 else "🔴" if c1 < -0.1 else "⚪"
                ps = f"${p:,.2f}" if p>=1000 else f"${p:,.4f}" if p>=1 else f"${p:,.6f}" if p>=0.01 else f"${p:,.8f}"
                msg += f"{i.get('icon','🔹')} <b>{c}</b>: {ps}\n  1h: {a1} {c1:+.2f}% | 24h: {c24:+.2f}%\n"
        send_msg(msg, main_kb(), cid)
    elif txt == "📈 الاستحواذ":
        send_msg("⏳ جاري الجلب...", cid=cid)
        dom = get_dominance(); fng = get_fng()
        msg = "📈 <b>الاستحواذ</b>\n━━━━━━━━━━━━━\n"
        if dom:
            msg += f"₿ BTC: {dom['btc']:.1f}%\n💵 USDT: {dom['usdt']:.1f}%\nΞ ETH: {dom['eth']:.1f}%\n"
            msg += f"💰 السوق: ${dom['total_mcap']/1e9:.0f}B ({dom['mcap_change']:+.2f}%)\n\n"
        if fng:
            v = fng["value"]
            msg += f"😱 {v}/100 - {fng['cls']}\n"
            msg += "🟩"*int(v/10) + "⬜"*(10-int(v/10)) + "\n\n"
        if dom and fng:
            v_val, usdt = fng["value"], dom["usdt"]
            if v_val < 30 and usdt > 5: msg += "💡 🔴 خوف + USDT مرتفع = هابط"
            elif v_val < 35 and usdt < 4.5: msg += "💡 🟢 خوف + USDT منخفض = فرصة شراء"
            elif v_val > 70 and usdt < 4: msg += "💡 🟡 جشع + USDT منخفض = قمة"
            elif usdt > 5.5: msg += "💡 🟡 USDT يرتفع"
            elif usdt < 4: msg += "💡 🟢 USDT ينخفض"
            else: msg += "💡 ⚪ متوازن"
        send_msg(msg, main_kb(), cid)
    elif txt == "🔔 التنبيهات":
        send_msg("🔔 <b>التنبيهات</b>\n✅ مفعّل | ➕ غير مفعّل", coins_kb(), cid)
    elif txt == "➕ إضافة عملة":
        _user_state[cid] = "waiting_for_coin"
        send_msg("➕ <b>أرسل رمز العملة</b>\nمثال: <code>DOGE</code> أو <code>SOL</code>", None, cid)
    elif txt == "❓ حالة":
        up = time.time() - _start_time
        h = int(up // 3600); m = int((up % 3600) // 60)
        send_msg(f"❓ <b>الحالة</b>\n🟢 يعمل\n⏱️ {h}س {m}د\n📊 {len(COINS)} عملة\n👁️ {len(watched_coins)} مراقبة", main_kb(), cid)
    else:
        send_msg("استخدم القائمة", main_kb(), cid)

def handle_coin_input(cid, text):
    _user_state[cid] = None
    symbol = text.upper().strip().replace("USDT", "").replace("/", "")
    if symbol in COINS:
        send_msg(f"ℹ️ {symbol} موجودة مسبقاً", main_kb(), cid)
        return
    send_msg(f"⏳ جاري البحث عن {symbol}...", None, cid)
    coin_id, coin_name = search_coin(symbol)
    if not coin_id:
        send_msg(f"❌ لم يتم العثور على {symbol}", main_kb(), cid)
        return
    COINS[symbol] = {"name_ar": coin_name, "icon": "🔹", "id": coin_id, "custom": True}
    watched_coins.add(symbol)
    price_history[symbol] = []
    save_custom()
    if "prices" in _cache:
        del _cache["prices"]
    send_msg(f"✅ <b>تمت إضافة {symbol}</b>\n🔹 {coin_name}", main_kb(), cid)

def handle_cb(cid, d, cb_id):
    global watched_coins
    if d.startswith("toggle_"):
        c = d.replace("toggle_", "")
        if c in watched_coins:
            watched_coins.discard(c)
        else:
            watched_coins.add(c)
        try:
            requests.post(f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "تم"}, timeout=10)
        except:
            pass
        send_msg("🔔 <b>التنبيهات</b>\n✅ مفعّل | ➕ غير مفعّل", coins_kb(), cid)
    elif d == "done":
        try:
            requests.post(f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "✅ تم"}, timeout=10)
        except:
            pass
        send_msg(f"✅ <b>تم الحفظ</b>\n👁️: {', '.join(sorted(watched_coins))}", main_kb(), cid)

def monitor_loop():
    global _last_usdt_dom
    time.sleep(10)
    while True:
        try:
            pr = get_prices()
            dom = get_dominance()
            u_r, u_f = False, False
            if dom and _last_usdt_dom is not None:
                diff = dom["usdt"] - _last_usdt_dom
                if diff > 0.1: u_r = True
                elif diff < -0.1: u_f = True
            if dom: _last_usdt_dom = dom["usdt"]
            for c in watched_coins:
                if c not in pr: continue
                d = pr.get(c)
                if not d or d["price"] == 0: continue
                p = d["price"]
                hist = price_history.get(c, [])
                if not hist: continue
                ref = hist[0][1]
                if ref == 0: continue
                ch = ((p - ref) / ref) * 100
                if abs(ch) >= ALERT_THRESHOLD:
                    info = COINS.get(c, {"name_ar": c})
                    e = "🟢" if ch > 0 else "🔴"
                    a = "📈" if ch > 0 else "📉"
                    m = f"{e} <b>تنبيه - {info.get('name_ar', c)} ({c})</b>\n━━━━━━━━━━━━━\n"
                    m += f"{a} <b>تغير:</b> {ch:+.2f}%\n💲 <b>السعر:</b> ${p:,.4f}\n\n"
                    if ch > 0 and u_f: m += "💡 🟢🟢 صعودي قوي"
                    elif ch < 0 and u_r: m += "💡 🔴🔴 هبوطي قوي"
                    else: m += "💡 📊 حركة سعرية"
                    m += f"\n\n🕐 {datetime.now(tz).strftime('%H:%M')}"
                    send_msg(m)
                    hist[0] = (time.time(), p)
            time.sleep(CHECK_INTERVAL)
        except:
            time.sleep(60)

def self_ping():
    if not RENDER_URL:
        return
    time.sleep(30)
    while True:
        try:
            requests.get(f"{RENDER_URL}/ping", timeout=10)
        except:
            pass
        time.sleep(600)

def send_daily():
    try:
        pr = get_prices()
        dom = get_dominance()
        fng = get_fng()
        msg = f"🌅 <b>ملخص - {datetime.now(tz).strftime('%Y-%m-%d')}</b>\n━━━━━━━━━━━━━\n\n"
        for c in sorted(COINS.keys()):
            d = pr.get(c)
            if d and d["price"] > 0:
                p = d["price"]; c24 = d.get("change_24h", 0)
                e = "🟢" if c24 >= 0 else "🔴"
                ps = f"${p:,.2f}" if p>=1000 else f"${p:,.4f}" if p>=1 else f"${p:,.6f}" if p>=0.01 else f"${p:,.8f}"
                msg += f"<b>{c}</b>: {ps} {e} {c24:+.2f}%\n"
        if dom:
            msg += f"\n₿ BTC: {dom['btc']:.1f}%\n💵 USDT: {dom['usdt']:.1f}%\n"
        if fng:
            msg += f"😱 {fng['value']}/100\n"
        send_msg(msg)
    except:
        pass

app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        u = request.get_json()
        if u:
            threading.Thread(target=handle_update, args=(u,)).start()
    except:
        pass
    return jsonify({"ok": True})

@app.route("/")
def home():
    return jsonify({"status": "running", "coins": list(COINS.keys())})

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route("/ping")
def ping():
    return jsonify({"pong": True})

def start_bot():
    global _started, _start_time
    if _started:
        return
    _started = True
    _start_time = time.time()
    if not TOKEN:
        return
    load_custom()
    wh = False
    if RENDER_URL:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TOKEN}/setWebhook", params={"url": f"{RENDER_URL}/webhook"}, timeout=10)
            if r.status_code == 200 and r.json().get("ok"):
                wh = True
        except:
            pass
    send_msg(f"🚀 <b>تم التشغيل</b>\n📊 عملات: {', '.join(sorted(COINS.keys()))}\n📥 أرسل /start")
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    def sched():
        schedule.every().day.at(f"{DAILY_HOUR:02d}:00").do(send_daily)
        while True:
            schedule.run_pending()
            time.sleep(60)
    threading.Thread(target=sched, daemon=True).start()
    if not wh:
        def poll():
            global last_id
            last_id = 0
            while True:
                try:
                    r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", params={"offset": last_id+1, "timeout": 25}, timeout=30)
                    if r.status_code == 200:
                        for u in r.json().get("result", []):
                            last_id = u.get("update_id", last_id)
                            handle_update(u)
                    else:
                        time.sleep(5)
                except:
                    time.sleep(5)
        threading.Thread(target=poll, daemon=True).start()

# هذا يبدأ البوت تلقائياً عند استيراد الملف بواسطة gunicorn
start_bot()
