"""
Complete bot.py for Telegram File Shop (SQLite + Google Drive links).
- Supports 3 payment providers (Zarinpal/IDPay/NextPay) as initiation links.
- If API keys are filled in config.json, bot will attempt server-side verification.
- Otherwise, it accepts a transaction id text from the user to mark purchase as paid.
Usage:
- Fill `config.json` with BOT token and optional gateway keys.
- Run: python bot.py
"""
import os
import json
import sqlite3
import secrets
import time
import logging

try:
    import requests
except Exception:
    requests = None

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

BASE_DIR = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
PRODUCTS_DIR = os.path.join(BASE_DIR, "products")
DB_PATH = os.path.join(BASE_DIR, "database.db")

# load config
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

BOT_TOKEN = CONFIG.get("bot_token") or os.getenv("BOT_TOKEN")
ADMIN_ID = int(CONFIG.get("admin_id") or 0)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# helper: db
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE,
                    username TEXT,
                    created_at INTEGER
                )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS purchases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    product_id TEXT,
                    plan TEXT,
                    provider TEXT,
                    provider_ref TEXT,
                    amount INTEGER,
                    success INTEGER DEFAULT 0,
                    created_at INTEGER
                )""")
    conn.commit()
    conn.close()

# load product
def load_product(pid):
    path = os.path.join(PRODUCTS_DIR, pid, "product.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# payment link builders (simple, not calling provider APIs to create invoice)
def zarinpal_create_link(amount_toman, callback_url, desc):
    authority = secrets.token_urlsafe(12)
    link = f"https://www.zarinpal.com/pg/StartPay/{authority}"
    return {"authority": authority, "link": link}

def idpay_create_link(amount_toman, callback_url, order_id, name=None):
    trans_id = secrets.token_urlsafe(10)
    link = f"https://idpay.ir/p/{trans_id}"
    return {"id": trans_id, "link": link}

def nextpay_create_link(amount_toman, callback_url):
    token = secrets.token_urlsafe(10)
    link = f"https://nextpay.org/nx/gateway/payment/{token}"
    return {"token": token, "link": link}

# verification helpers: try provider APIs if configured, otherwise accept user-submitted txid as proof
def verify_with_provider(provider, ref, amount):
    cfg = CONFIG
    # if requests not available, cannot verify via API
    if requests is None:
        return False
    try:
        if provider == "zarinpal" and cfg.get("zarinpal_merchant_id"):
            payload = {"merchant_id": cfg.get("zarinpal_merchant_id"), "authority": ref, "amount": amount*10}
            r = requests.post("https://api.zarinpal.com/pg/v4/payment/verify.json", json=payload, timeout=10)
            j = r.json()
            data = j.get("data") or {}
            return data.get("code") in (100, 101)
        if provider == "idpay" and cfg.get("idpay_api_key"):
            headers = {"X-API-KEY": cfg.get("idpay_api_key"), "Content-Type":"application/json"}
            r = requests.post("https://api.idpay.ir/v1.1/payment/verify", json={"id":ref}, headers=headers, timeout=10)
            j = r.json()
            return j.get("status") == 100
        if provider == "nextpay" and cfg.get("nextpay_api_key"):
            r = requests.post("https://nextpay.org/nx/gateway/verify", json={"token":ref, "api_key":cfg.get("nextpay_api_key")}, timeout=10)
            j = r.json()
            return j.get("status") == 1
    except Exception as e:
        logger.warning("Provider verification error: %s", e)
    return False

# bot handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (telegram_id, username, created_at) VALUES (?,?,?)",
                (user.id, user.username or "", int(time.time())))
    conn.commit()
    conn.close()
    await update.message.reply_text("سلام! به فروشگاه فایل خوش آمدی. برای دیدن محصولات /products را بزن.")

async def products_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = []
    for name in sorted(os.listdir(PRODUCTS_DIR)):
        pdir = os.path.join(PRODUCTS_DIR, name)
        if os.path.isdir(pdir):
            try:
                prod = load_product(name)
                items.append((name, prod.get("title")))
            except Exception:
                continue
    if not items:
        await update.message.reply_text("فعلاً محصولی وجود ندارد.")
        return
    text = "محصولات موجود:\n"
    for pid, title in items:
        text += f"{pid}. {title} — /product_{pid}\n"
    await update.message.reply_text(text)

async def product_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = update.message.text.strip().lstrip("/")
    parts = cmd.split("_",1)
    if len(parts) != 2:
        await update.message.reply_text("فرمت دستور اشتباه است.")
        return
    pid = parts[1]
    try:
        prod = load_product(pid)
    except Exception:
        await update.message.reply_text("محصول پیدا نشد.")
        return
    img_path = os.path.join(PRODUCTS_DIR, pid, prod.get("cover_image"))
    caption = f"*{prod.get('title')}*\n\n{prod.get('description')}\n\n"
    econ = prod["plans"]["economic"]
    gold = prod["plans"]["golden"]
    caption += f"🟢 نسخه اقتصادی: {econ['price']} تومان\n🔶 نسخه طلایی: {gold['price']} تومان\n"
    keyboard = [
        [InlineKeyboardButton(f"🛒 {econ['name']} — {econ['price']} تومان", callback_data=f"buy|{pid}|economic")],
        [InlineKeyboardButton(f"💎 {gold['name']} — {gold['price']} تومان", callback_data=f"buy|{pid}|golden")]
    ]
    if os.path.exists(img_path):
        await update.message.reply_photo(photo=open(img_path,"rb"), caption=caption, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(caption, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def callback_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split("|")
    if len(parts) != 3:
        await query.message.reply_text("دادهٔ نامعتبر.")
        return
    _, pid, plan = parts
    prod = load_product(pid)
    planinfo = prod["plans"][plan]
    amount = planinfo["price"]
    keyboard = [
        [InlineKeyboardButton("زرین‌پال", callback_data=f"startpay|{pid}|{plan}|zarinpal")],
        [InlineKeyboardButton("IDPay", callback_data=f"startpay|{pid}|{plan}|idpay")],
        [InlineKeyboardButton("NextPay", callback_data=f"startpay|{pid}|{plan}|nextpay")]
    ]
    await query.message.reply_text(f"میزان: {amount} تومان\nدرگاه مدنظر را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))

async def startpay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, pid, plan, provider = query.data.split("|")
    prod = load_product(pid)
    planinfo = prod["plans"][plan]
    amount = planinfo["price"]
    callback_url = ""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE telegram_id=?", (query.from_user.id,))
    row = cur.fetchone()
    if row:
        user_id = row["id"]
    else:
        cur.execute("INSERT INTO users (telegram_id, username, created_at) VALUES (?,?,?)", (query.from_user.id, query.from_user.username or "", int(time.time())))
        user_id = cur.lastrowid
    created_at = int(time.time())
    provider_ref = ""
    if provider == "zarinpal":
        res = zarinpal_create_link(amount, callback_url, prod.get("title"))
        provider_ref = res["authority"]
        pay_link = res["link"]
    elif provider == "idpay":
        res = idpay_create_link(amount, callback_url, order_id=str(int(time.time())), name=prod.get("title"))
        provider_ref = res["id"]
        pay_link = res["link"]
    else:
        res = nextpay_create_link(amount, callback_url)
        provider_ref = res["token"]
        pay_link = res["link"]
    cur.execute("INSERT INTO purchases (user_id, product_id, plan, provider, provider_ref, amount, success, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (user_id, pid, plan, provider, provider_ref, amount, 0, created_at))
    purchase_id = cur.lastrowid
    conn.commit()
    conn.close()
    text = f"برای پرداخت به این لینک بروید:\n{pay_link}\n\nپس از پرداخت، شمارهٔ تراکنش یا کد تراکنش را اینجا ارسال کنید (مثلاً: `تراکنش {purchase_id} 123456`)."
    await query.message.reply_text(text)

async def receipt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not txt:
        return
    parts = txt.split()
    purchase_id = None
    txid = None
    if parts[0].lower() == "تراکنش" and len(parts) >= 3:
        try:
            purchase_id = int(parts[1])
            txid = parts[2]
        except:
            pass
    elif len(parts) == 2 and parts[0].isdigit():
        try:
            purchase_id = int(parts[0])
            txid = parts[1]
        except:
            pass
    else:
        txid = parts[-1]
    conn = get_db()
    cur = conn.cursor()
    if purchase_id is None:
        cur.execute("SELECT * FROM purchases WHERE success=0 ORDER BY created_at DESC")
        row = cur.fetchone()
        if row:
            purchase_id = row["id"]
        else:
            await update.message.reply_text("خرید در حال انتظار پیدا نشد. لطفا ابتدا یک محصول بخرید.")
            conn.close()
            return
    cur.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,))
    p = cur.fetchone()
    if not p:
        await update.message.reply_text("خرید مورد نظر یافت نشد. شناسه خرید را بررسی کن.")
        conn.close()
        return
    provider = p["provider"]
    amount = p["amount"]
    verified = False
    if verify_with_provider(provider, txid, amount):
        verified = True
    else:
        cfg = CONFIG
        keys_present = cfg.get("zarinpal_merchant_id") or cfg.get("idpay_api_key") or cfg.get("nextpay_api_key")
        if not keys_present:
            verified = True
    if verified:
        cur.execute("UPDATE purchases SET success=1, provider_ref=? WHERE id=?", (txid, purchase_id))
        conn.commit()
        prod = load_product(p["product_id"])
        planinfo = prod["plans"][p["plan"]]
        download_link = planinfo["download_link"]
        await update.message.reply_text("پرداخت تایید شد. این هم لینک دانلود شما (مستقیم):\n" + download_link)
        conn.close()
    else:
        await update.message.reply_text("پرداخت تأیید نشد. اگر پرداخت موفق بوده، لطفاً رسید یا شمارهٔ تراکنش را ارسال کنید یا با پشتیبانی تماس بگیرید.")
        conn.close()

async def admin_list_purchases(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != int(CONFIG.get("admin_id") or 0):
        await update.message.reply_text("فقط ادمین مجاز است.")
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT p.id, u.telegram_id, p.product_id, p.plan, p.amount, p.success, p.provider_ref, p.created_at FROM purchases p LEFT JOIN users u ON p.user_id=u.id ORDER BY p.created_at DESC")
    rows = cur.fetchall()
    if not rows:
        await update.message.reply_text("خریدی ثبت نشده.")
        conn.close()
        return
    text = "خریدها:\n"
    for r in rows[:50]:
        ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r[7] if isinstance(r[7], int) else r[7]))
        text += f"#{r[0]} user:{r[1]} product:{r[2]} plan:{r[3]} amount:{r[4]} success:{r[5]} ref:{r[6]} time:{ts}\n"
    await update.message.reply_text(text)
    conn.close()

async def admin_set_gateway(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != int(CONFIG.get("admin_id") or 0):
        await update.message.reply_text("فقط ادمین مجاز است.")
        return
    parts = update.message.text.strip().split()
    if len(parts) != 2:
        await update.message.reply_text("فرمت: /setgateway <zarinpal|idpay|nextpay>")
        return
    gw = parts[1].lower()
    if gw not in ("zarinpal","idpay","nextpay"):
        await update.message.reply_text("درگاه نامعتبر.")
        return
    CONFIG["payment_gateway"] = gw
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, ensure_ascii=False, indent=2)
    await update.message.reply_text(f"درگاه پیش‌فرض به {gw} تغییر یافت.")

def main():
    init_db()
    if not BOT_TOKEN:
        print("Bot token not set. Put it in config.json as 'bot_token' or set BOT_TOKEN env var.")
        return
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("products", products_handler))
    app.add_handler(CallbackQueryHandler(callback_buy, pattern=r"^buy\|"))
    app.add_handler(CallbackQueryHandler(startpay_handler, pattern=r"^startpay\|"))
    app.add_handler(CommandHandler("product_01", product_view))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), receipt_handler))
    app.add_handler(CommandHandler("listpurchases", admin_list_purchases))
    app.add_handler(CommandHandler("setgateway", admin_set_gateway))
    print("Bot is starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
