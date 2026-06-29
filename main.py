import os, sys, time, logging, threading, schedule
from datetime import datetime
import pytz, requests
from flask import Flask, jsonify

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TIMEZONE = os.environ.get("TIMEZONE", "Africa/Algiers")
PORT = int(os.environ.get("PORT", 10000))

COINS = {
    "BTC":  {"name_ar": "بيتكوين",  "icon": "₿"},
    "ETH":  {"name_ar": "إيثيريوم", "icon": "Ξ"},
    "SKL":  {"name_ar": "سكيل",     "icon": "❖"},
    "ROSE": {"name_ar": "أواسيس",   "icon": "✿"},
    "APT":  {"name_ar": "أبتوس",    "icon": "◆"},
}
ALERT_THRESHOLD_PCT = float(os.environ.get("ALERT_THRESHOLD_PCT", "2.0"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))
DAILY_SUMMARY_HOUR = int(os.environ.get("DAILY_SUMMARY_HOUR", "8"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("Bot")
tz = pytz.timezone(TIMEZONE)
_cache = {}

def get_prices():
    if "prices" in _cache and time.time() - _cache["prices"][0] < 300:
        return _cache["prices"][1]
    result = {}
    for page in [1, 2]:
        try:
            r = requests.get("https://api.coingecko.com/api/v3/coins/markets",
                params={"vs_currency":"usd","order":"market_cap_desc","per_page":250,"page":page,"price_change_percentage":"24h"},
                timeout=15, headers={"User-Agent":"Mozilla/5.0"})
            if r.status_code == 200:
                for c in r.json():
                    s = c.get("symbol","").upper()
                    if s: result[s] = {"price":c.get("current_price",0),"change_24h":c.get("price_change_percentage_24h",0) or 0}
            time.sleep(1)
        except: pass
    try:
        r = requests.get("https://api.coinbase.com/v2/prices/SKL-USD/spot", timeout=10)
        if r.status_code == 200: result["SKL"] = {"price":float(r.json()["data"]["amount"]),"change_24h":0}
    except: pass
    _cache["prices"] = (time.time(), result)
    return result

def get_dominance():
    if "dom" in _cache and time.time() - _cache["dom"][0] < 3600: return _cache["dom"][1]
    try:
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=15)
        if r.status_code == 200:
            d = r.json()["data"]
            result = {"btc_dominance":d.get("market_cap_percentage",{}).get("btc",0),"eth_dominance":d.get("market_cap_percentage",{}).get("eth",0),"total_market_cap":d.get("total_market_cap",{}).get("usd",0),"market_cap_change_24h":d.get("market_cap_change_percentage_24h_usd",0)}
            _cache["dom"] = (time.time(), result)
            return result
    except: pass
    return None

def get_fng():
    if "fng" in _cache and time.time() - _cache["fng"][0] < 3600: return _cache["fng"][1]
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        if r.status_code == 200:
            d = r.json()["data"][0]
            cls = {"Extreme Fear":"😨 خوف شديد","Fear":"😟 خوف","Neutral":"😐 محايد","Greed":"😄 جشع","Extreme Greed":"🤑 جشع شديد"}.get(d["value_classification"],d["value_classification"])
            result = {"value":int(d["value"]),"classification_ar":cls}
            _cache["fng"] = (time.time(), result)
            return result
    except: pass
    return None

def send_telegram(msg, reply_markup=None, chat_id=None):
    target = chat_id or TELEGRAM_CHAT_ID
    if not target or not TELEGRAM_BOT_TOKEN: return
    try:
        payload = {"chat_id":target,"text":msg,"parse_mode":"HTML","disable_web_page_preview":True}
        if reply_markup: payload["reply_markup"] = reply_markup
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload, timeout=15)
    except Exception as e: logger.error(f"خطأ: {e}")

class Monitor:
    def __init__(self):
        self.ref_prices = {}
        self.last_alert = {}
        self.alerts_enabled = True
        self.start_time = time.time()
    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        logger.info(f"👁️ مراقب يعمل (حد {ALERT_THRESHOLD_PCT}%)")
    def toggle(self):
        self.alerts_enabled = not self.alerts_enabled
        return self.alerts_enabled
    def _loop(self):
        while True:
            try:
                if self.alerts_enabled:
                    prices = get_prices()
                    for code in COINS: self._check(code, prices)
                time.sleep(CHECK_INTERVAL)
            except Exception as e:
                logger.error(f"خطأ: {e}")
                time.sleep(60)
    def _check(self, code, prices):
        data = prices.get(code)
        if not data: return
        price = data["price"]
        ref = self.ref_prices.get(code)
        if ref is None: self.ref_prices[code] = price; return
        change = ((price - ref) / ref) * 100
        if abs(change) >= ALERT_THRESHOLD_PCT:
            now = time.time()
            if now - self.last_alert.get(code, 0) < 3600: return
            info = COINS[code]
            emoji = "🟢" if change > 0 else "🔴"
            arrow = "📈" if change > 0 else "📉"
            def fmt(p):
                if p >= 1000: return f"${p:,.2f}"
                elif p >= 1: return f"${p:,.4f}"
                elif p >= 0.01: return f"${p:,.6f}"
                else: return f"${p:,.8f}"
            msg = f"{emoji} <b>تنبيه - {info['name_ar']} ({code})</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            msg += f"{arrow} <b>التغير:</b> {change:+.2f}%\n\n"
            msg += f"💲 <b>السابق:</b> {fmt(ref)}\n💲 <b>الحالي:</b> {fmt(price)}\n\n"
            msg += f"🕐 {datetime.now(tz).strftime('%Y-%m-%d %H:%M')}\n"
            if abs(change) >= 5: msg += "⚡ حركة قوية\n"
            elif abs(change) >= 3: msg += "📊 حركة متوسطة\n"
            msg += "\n⚠️ <i>ليس نصيحة استثمارية</i>"
            send_telegram(msg)
            self.last_alert[code] = now
            self.ref_prices[code] = price
        elif abs(change) < 0.5:
            self.ref_prices[code] = price

monitor = Monitor()

def get_keyboard():
    btn = "🔕 إيقاف التنبيهات" if monitor.alerts_enabled else "🔔 تشغيل التنبيهات"
    return {"keyboard":[[{"text":"📊 الأسعار"},{"text":"📈 الاستحواذ"}],[{"text":btn},{"text":"❓ حالة البوت"}]],"resize_keyboard":True,"is_persistent":True}

def handle_message(chat_id, text):
    if text == "/start":
        msg = "🤖 <b>مرحباً بك</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n📊 بوت متابعة العملات\n"
        msg += f"👁️ تنبيه عند ≥ <b>{ALERT_THRESHOLD_PCT}%</b>\n\n🎯 <b>العملات:</b> {', '.join(COINS.keys())}"
        send_telegram(msg, get_keyboard(), chat_id)
    elif text == "📊 الأسعار":
        send_telegram("⏳ جاري جلب الأسعار...", chat_id=chat_id)
        prices = get_prices()
        msg = "📊 <b>أسعار العملات</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for code, info in COINS.items():
            d = prices.get(code)
            if d:
                p = d["price"]; c = d.get("change_24h",0)
                e = "🟢" if c >= 0 else "🔴"
                ps = f"${p:,.2f}" if p >= 1000 else f"${p:,.4f}" if p >= 1 else f"${p:,.6f}" if p >= 0.01 else f"${p:,.8f}"
                msg += f"{info['icon']} <b>{code}</b>: {ps} {e} {c:+.2f}%\n"
        send_telegram(msg, get_keyboard(), chat_id)
    elif text == "📈 الاستحواذ":
        send_telegram("⏳ جاري جلب البيانات...", chat_id=chat_id)
        dom = get_dominance(); fng = get_fng()
        msg = "📈 <b>الاستحواذ ومؤشرات السوق</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        if dom:
            msg += f"₿ <b>BTC:</b> {dom['btc_dominance']:.1f}%\n"
            msg += f"Ξ <b>ETH:</b> {dom['eth_dominance']:.1f}%\n"
            msg += f"💰 <b>السوق:</b> ${dom['total_market_cap']/1e9:,.0f}B\n"
            ch = dom.get("market_cap_change_24h",0)
            msg += f"{'🟢' if ch >= 0 else '🔴'} <b>تغير 24h:</b> {ch:+.2f}%\n\n"
        if fng:
            v = fng["value"]; bp = int(v/10)
            msg += f"😱 <b>الخوف/الجشع:</b> {v}/100\n📋 {fng['classification_ar']}\n"
            msg += "🟩"*bp + "⬜"*(10-bp) + f" {v}/100\n\n"
        if dom and fng:
            if fng["value"] < 30 and dom["market_cap_change_24h"] < 0: msg += "💡 فرصة شراء محتملة"
            elif fng["value"] > 70 and dom["market_cap_change_24h"] > 0: msg += "💡 حذر من التصحيح"
            else: msg += "💡 السوق متوازن"
        send_telegram(msg, get_keyboard(), chat_id)
    elif text in ("🔔 تشغيل التنبيهات", "🔕 إيقاف التنبيهات"):
        en = monitor.toggle()
        msg = "🔔 <b>التنبيهات مفعّلة</b>" if en else "🔕 <b>التنبيهات متوقفة</b>"
        send_telegram(msg, get_keyboard(), chat_id)
    elif text == "❓ حالة البوت":
        up = time.time() - monitor.start_time
        h = int(up//3600); m = int((up%3600)//60)
        st = "🔔 مفعّلة" if monitor.alerts_enabled else "🔕 متوقفة"
        msg = f"❓ <b>حالة البوت</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n🟢 <b>يعمل</b>\n⏱️ {h}س {m}د\n{st} <b>التنبيهات</b>\n📊 {len(COINS)} عملات\n🎯 حد {ALERT_THRESHOLD_PCT}%\n\n🕐 {datetime.now(tz).strftime('%H:%M:%S')}"
        send_telegram(msg, get_keyboard(), chat_id)
    else:
        send_telegram("استخدم القائمة", get_keyboard(), chat_id)

def send_daily():
    try:
        prices = get_prices()
        msg = f"🌅 <b>ملخص - {datetime.now(tz).strftime('%Y-%m-%d')}</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for code, info in COINS.items():
            d = prices.get(code)
            if d:
                p = d["price"]; c = d.get("change_24h",0)
                e = "🟢" if c >= 0 else "🔴"
                ps = f"${p:,.2f}" if p >= 1000 else f"${p:,.4f}" if p >= 1 else f"${p:,.6f}" if p >= 0.01 else f"${p:,.8f}"
                msg += f"{info['icon']} <b>{code}</b>: {ps} {e} {c:+.2f}%\n"
        dom = get_dominance()
        if dom: msg += f"\n₿ BTC.D: {dom['btc_dominance']:.1f}%\n"
        fng = get_fng()
        if fng: msg += f"😱 {fng['value']}/100 - {fng['classification_ar']}\n"
        send_telegram(msg)
    except Exception as e: logger.error(f"خطأ: {e}")

last_update_id = 0
def bot_polling():
    global last_update_id
    logger.info("🤖 polling يعمل")
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={"offset":last_update_id+1,"timeout":25}, timeout=30)
            if r.status_code == 200:
                for u in r.json().get("result",[]):
                    last_update_id = u.get("update_id", last_update_id)
                    m = u.get("message",{})
                    if m:
                        cid = m.get("chat",{}).get("id"); txt = m.get("text","").strip()
                        if cid and txt:
                            logger.info(f"📩 {txt}")
                            try: handle_message(cid, txt)
                            except Exception as e: logger.error(f"خطأ: {e}")
            else: time.sleep(5)
        except Exception as e:
            logger.error(f"خطأ polling: {e}")
            time.sleep(5)

app = Flask(__name__)
@app.route("/")
def home(): return jsonify({"status":"running","bot":"active","coins":list(COINS.keys())})
@app.route("/health")
def health(): return jsonify({"status":"ok"})
@app.route("/ping")
def ping(): return jsonify({"pong":True,"time":datetime.now(tz).isoformat()})

def start_bot():
    if not TELEGRAM_BOT_TOKEN: return
    send_telegram("🚀 <b>تم تشغيل البوت</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n📥 أرسل /start")
    monitor.start()
    def sched_loop():
        schedule.every().day.at(f"{DAILY_SUMMARY_HOUR:02d}:00").do(send_daily)
        while True: schedule.run_pending(); time.sleep(60)
    threading.Thread(target=sched_loop, daemon=True).start()
    threading.Thread(target=bot_polling, daemon=True).start()

if __name__ == "__main__":
    start_bot()
    app.run(host="0.0.0.0", port=PORT)
