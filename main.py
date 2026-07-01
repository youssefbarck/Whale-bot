import os, time, json, logging, threading, schedule
from datetime import datetime
import pytz, requests
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

CHECK_INTERVAL = 300
ALERT_THRESHOLD = 2.0
ALERT_COOLDOWN = 1800

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("Bot")
tz = pytz.timezone(TIMEZONE)
HEADERS = {"User-Agent": "Mozilla/5.0"}

_cache = {}
price_history = {}
watched_coins = set()
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
    if not t or not TOKEN: return
    try:
        p = {"chat_id": t, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True}
        if kb: p["reply_markup"] = json.dumps(kb) if isinstance(kb, dict) else kb
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json=p, timeout=15)
    except: pass

def main_kb():
    return {"keyboard": [
        [{"text": "📊 المفضلة"}, {"text": "📋 القائمة العامة"}],
        [{"text": "🔔 التنبيهات"}, {"text": "➕ إضافة عملة"}]
    ], "resize_keyboard": True, "is_persistent": True}

def coins_kb():
    rows, row = [], []
    for c in sorted(COINS.keys()):
        i = "✅" if c in watched_coins else "➕"
        row.append({"text": f"{i} {c}", "callback_data": f"toggle_{c}"})
        if len(row) == 3: rows.append(row); row = []
    if row: rows.append(row)
    rows.append([{"text": "✅ تم", "callback_data": "done"}])
    return {"inline_keyboard": rows}

def handle_update(u):
    m = u.get("message", {})
    if m:
        cid = m.get("chat", {}).get("id")
        txt = m.get("text", "").strip()
        if cid and txt: handle_msg(cid, txt)
        return
    cb = u.get("callback_query", {})
    if cb:
        cid = cb.get("message", {}).get("chat", {}).get("id")
        d = cb.get("data", "")
        cb_id = cb.get("id", "")
        if cid and d: handle_cb(cid, d, cb_id)

def build_prices_msg():
    pr = get_prices()
    msg = "📊 <b>المفضلة</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━\n\n"
    for c in sorted(COINS.keys()):
        i = COINS[c]
        d = pr.get(c)
        if d and d["price"] > 0:
            p = d["price"]; c24 = d.get("change_24h", 0); c1 = get_1h_change(c)
            a1 = "🟢" if c1 > 0.1 else "🔴" if c1 < -0.1 else "⚪"
            ps = f"${p:,.2f}" if p>=1000 else f"${p:,.4f}" if p>=1 else f"${p:,.6f}" if p>=0.01 else f"${p:,.8f}"
            # رابط TradingView clickable
            tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE%3A{c}USDT"
            msg += f"{i.get('icon','🔹')} <a href='{tv_url}'>{c}</a>: {ps}\n"
            msg += f"   ┣ 📈 1h: {a1} {c1:+.2f}% | 24h: {c24:+.2f}%\n\n"
    return msg

def build_general_msg():
    pr = get_prices()
    msg = "📋 <b>القائمة العامة</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━\n\n"
    for c in sorted(COINS.keys()):
        i = COINS[c]
        d = pr.get(c)
        if d and d["price"] > 0:
            p = d["price"]; c24 = d.get("change_24h", 0)
            e = "🟢" if c24 >= 0 else "🔴"
            ps = f"${p:,.2f}" if p>=1000 else f"${p:,.4f}" if p>=1 else f"${p:,.6f}" if p>=0.01 else f"${p:,.8f}"
            watch = "👁️" if c in watched_coins else "➖"
            tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE%3A{c}USDT"
            msg += f"{watch} {i.get('icon','🔹')} <a href='{tv_url}'>{c}</a>: {ps} {e} {c24:+.2f}%\n"
    msg += "\n👁️ = تحت المراقبة | ➖ = غير مراقبة"
    return msg

def handle_msg(cid, txt):
    if _user_state.get(cid) == "waiting_for_coin":
        handle_coin_input(cid, txt)
        return
    if txt == "/start":
        msg = "🤖 <b>مرحباً</b>\n📊 بوت متابعة العملات\n"
        msg += f"🎯 عملاتك: {', '.join(sorted(COINS.keys()))}"
        send_msg(msg, main_kb(), cid)
    elif txt == "📊 المفضلة":
        send_msg("⏳ جاري الجلب...", cid=cid)
        send_msg(build_prices_msg(), main_kb(), cid)
    elif txt == "📋 القائمة العامة":
        send_msg("⏳ جاري الجلب...", cid=cid)
        send_msg(build_general_msg(), main_kb(), cid)
    elif txt == "🔔 التنبيهات":
        msg = "🔔 <b>إدارة التنبيهات</b>\n"
        msg += "━━━━━━━━━━━━━━━━━━\n\n"
        msg += "⚠️ تنبيه فقط عند تغير مفاجئ ≥ 2%\n\n"
        msg += "✅ = مفعّل | ➕ = غير مفعّل\n"
        send_msg(msg, coins_kb(), cid)
    elif txt == "➕ إضافة عملة":
        _user_state[cid] = "waiting_for_coin"
        msg = "➕ <b>إضافة عملة جديدة</b>\n"
        msg += "━━━━━━━━━━━━━━━━━━\n\n"
        msg += "📝 أرسل رمز العملة:\n"
        msg += "مثال: <code>DOGE</code> أو <code>SOL</code>"
        send_msg(msg, None, cid)
    else:
        send_msg("استخدم القائمة بالأسفل", main_kb(), cid)

def handle_coin_input(cid, text):
    _user_state[cid] = None
    symbol = text.upper().strip().replace("USDT", "").replace("/", "")
    if symbol in COINS:
        send_msg(f"ℹ️ <b>{symbol}</b> موجودة مسبقاً", main_kb(), cid)
        return
    send_msg(f"⏳ جاري البحث عن <b>{symbol}</b>...", None, cid)
    coin_id, coin_name = search_coin(symbol)
    if not coin_id:
        send_msg(f"❌ لم يتم العثور على <b>{symbol}</b>", main_kb(), cid)
        return
    COINS[symbol] = {"name_ar": coin_name, "icon": "🔹", "id": coin_id, "custom": True}
    watched_coins.add(symbol)
    price_history[symbol] = []
    save_custom()
    if "prices" in _cache: del _cache["prices"]
    msg = f"✅ <b>تمت إضافة {symbol}</b>\n🔹 {coin_name}"
    send_msg(msg, main_kb(), cid)

def handle_cb(cid, d, cb_id):
    global watched_coins
    if d.startswith("toggle_"):
        c = d.replace("toggle_", "")
        if c in watched_coins: watched_coins.discard(c)
        else: watched_coins.add(c)
        try: requests.post(f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "تم"}, timeout=10)
        except: pass
        msg = "🔔 <b>إدارة التنبيهات</b>\n━━━━━━━━━━━━━━━━━━\n✅ مفعّل | ➕ غير مفعّل\n"
        send_msg(msg, coins_kb(), cid)
    elif d == "done":
        try: requests.post(f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "✅ تم"}, timeout=10)
        except: pass
        wl = ", ".join(sorted(watched_coins)) or "لا أحد"
        send_msg(f"✅ <b>تم الحفظ</b>\n👁️: {wl}", main_kb(), cid)

def monitor_loop():
    time.sleep(10)
    last_alerts = {}
    while True:
        try:
            pr = get_prices()
            now = time.time()
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
                    if now - last_alerts.get(c, 0) < ALERT_COOLDOWN:
                        continue
                    last_alerts[c] = now
                    info = COINS.get(c, {"name_ar": c})
                    e = "🟢" if ch > 0 else "🔴"
                    a = "📈" if ch > 0 else "📉"
                    m = f"{e} <b>تنبيه - {info.get('name_ar', c)} ({c})</b>\n"
                    m += "━━━━━━━━━━━━━━━━━━\n\n"
                    m += f"{a} <b>التغير المفاجئ:</b> {ch:+.2f}%\n"
                    m += f"💲 <b>السعر:</b> ${p:,.4f}\n\n"
                    if abs(ch) >= 5: m += "⚡ <b>حركة قوية جداً!</b>\n"
                    elif abs(ch) >= 3: m += "📊 <b>حركة قوية</b>\n"
                    else: m += "📉 <b>حركة ملحوظة</b>\n"
                    m += f"\n🕐 {datetime.now(tz).strftime('%Y-%m-%d %H:%M')}"
                    m += "\n\n⚠️ <i>ليس نصيحة استثمارية</i>"
                    send_msg(m)
                    hist[0] = (time.time(), p)
            time.sleep(CHECK_INTERVAL)
        except:
            time.sleep(60)

def self_ping():
    if not RENDER_URL: return
    time.sleep(30)
    while True:
        try: requests.get(f"{RENDER_URL}/ping", timeout=10)
        except: pass
        time.sleep(600)

app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        u = request.get_json()
        if u: threading.Thread(target=handle_update, args=(u,)).start()
    except: pass
    return jsonify({"ok": True})

@app.route("/")
def home(): return jsonify({"status": "running", "coins": list(COINS.keys())})
@app.route("/health")
def health(): return jsonify({"status": "ok"})
@app.route("/ping")
def ping(): return jsonify({"pong": True})

def start_bot():
    global _started, _start_time
    if _started: return
    _started = True
    _start_time = time.time()
    if not TOKEN: return
    load_custom()
    wh = False
    if RENDER_URL:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TOKEN}/setWebhook", params={"url": f"{RENDER_URL}/webhook"}, timeout=10)
            if r.status_code == 200 and r.json().get("ok"): wh = True
        except: pass
    msg = "🚀 <b>تم التشغيل</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━\n\n"
    msg += f"📊 <b>العملات:</b> {', '.join(sorted(COINS.keys()))}\n"
    msg += f"🔔 تنبيه عند تغير مفاجئ ≥ {ALERT_THRESHOLD}%\n\n"
    msg += "📥 أرسل /start للبدء"
    send_msg(msg)
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
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
                    else: time.sleep(5)
                except: time.sleep(5)
        threading.Thread(target=poll, daemon=True).start()

start_bot()
