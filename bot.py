import os
import time
import random
import logging
from threading import Thread

import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from flask import Flask, jsonify

from telegram import Update, Poll
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PollAnswerHandler,
    filters,
)


# =========================================================
# SETTINGS
# =========================================================

TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

ADMIN_ID = 6235380364

PORT = int(os.environ.get("PORT", 10000))

# Coin mining
COINS_PER_MESSAGE = 10
COOLDOWN = 2 * 60

# Quiz
QUIZ_REWARD = 10
QUIZ_COOLDOWN = 30 * 60  # 30 minutes

# AngryCoin
DEFAULT_ANGRYCOIN_PRICE = 100


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# DATABASE POOL
# =========================================================

db_pool = None


def create_db_pool():
    global db_pool

    if db_pool is not None:
        return

    db_pool = ThreadedConnectionPool(
        minconn=1,
        maxconn=10,
        dsn=DATABASE_URL,
        connect_timeout=10,
        sslmode="require",
    )

    logger.info("PostgreSQL pool created.")


def get_db():
    global db_pool

    if db_pool is None:
        create_db_pool()

    return db_pool.getconn()


def release_db(conn):
    if conn is None:
        return

    try:
        db_pool.putconn(conn)
    except Exception:
        logger.exception("Could not release DB connection.")


# =========================================================
# DATABASE INIT
# =========================================================

def init_db():
    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        # -----------------------------------------
        # USERS
        # -----------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                name TEXT NOT NULL,
                coins BIGINT NOT NULL DEFAULT 0,
                last_message DOUBLE PRECISION NOT NULL DEFAULT 0
            )
        """)

        # -----------------------------------------
        # BOT GROUPS
        # -----------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_groups (
                chat_id BIGINT PRIMARY KEY,
                title TEXT NOT NULL
            )
        """)

        # -----------------------------------------
        # QUIZ QUESTIONS
        # -----------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS quiz_questions (
                id SERIAL PRIMARY KEY,
                question TEXT NOT NULL,
                option1 TEXT NOT NULL,
                option2 TEXT NOT NULL,
                option3 TEXT NOT NULL,
                option4 TEXT NOT NULL,
                correct_option INTEGER NOT NULL
                    CHECK (correct_option BETWEEN 0 AND 3),
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                created_at DOUBLE PRECISION NOT NULL
            )
        """)

        # -----------------------------------------
        # QUIZ POLLS
        # -----------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS quiz_polls (
                poll_id TEXT PRIMARY KEY,
                question_id INTEGER NOT NULL
                    REFERENCES quiz_questions(id)
                    ON DELETE CASCADE,
                chat_id BIGINT NOT NULL,
                message_id BIGINT,
                created_at DOUBLE PRECISION NOT NULL
            )
        """)

        # -----------------------------------------
        # QUIZ ANSWERS
        # -----------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS quiz_answers (
                poll_id TEXT NOT NULL
                    REFERENCES quiz_polls(poll_id)
                    ON DELETE CASCADE,

                user_id BIGINT NOT NULL,

                selected_option INTEGER,
                is_correct BOOLEAN NOT NULL,
                coins_awarded INTEGER NOT NULL DEFAULT 0,
                answered_at DOUBLE PRECISION NOT NULL,

                PRIMARY KEY (poll_id, user_id)
            )
        """)

        # -----------------------------------------
        # QUIZ COOLDOWN
        # -----------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS quiz_cooldowns (
                chat_id BIGINT PRIMARY KEY,
                last_question_at DOUBLE PRECISION NOT NULL DEFAULT 0
            )
        """)

        # -----------------------------------------
        # QUIZ STATS
        # -----------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS quiz_user_stats (
                user_id BIGINT PRIMARY KEY,
                answered INTEGER NOT NULL DEFAULT 0,
                correct INTEGER NOT NULL DEFAULT 0,
                coins_earned BIGINT NOT NULL DEFAULT 0
            )
        """)

        # -----------------------------------------
        # ANGRYCOIN MARKET
        # -----------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS market (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                symbol TEXT NOT NULL,
                price NUMERIC(20,4) NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )
        """)

        # -----------------------------------------
        # HOLDINGS
        # -----------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_holdings (
                user_id BIGINT PRIMARY KEY,
                amount NUMERIC(20,4) NOT NULL DEFAULT 0
            )
        """)

        # -----------------------------------------
        # MARKET HISTORY
        # -----------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_history (
                id BIGSERIAL PRIMARY KEY,
                price NUMERIC(20,4) NOT NULL,
                created_at DOUBLE PRECISION NOT NULL
            )
        """)

        # -----------------------------------------
        # MARKET GROUPS
        # -----------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_groups (
                chat_id BIGINT PRIMARY KEY,
                title TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE
            )
        """)

        # -----------------------------------------
        # DEFAULT MARKET
        # -----------------------------------------

        cur.execute("""
            INSERT INTO market (
                id,
                name,
                symbol,
                price,
                updated_at
            )
            VALUES (
                1,
                'AngryCoin',
                'ANGRY',
                %s,
                %s
            )
            ON CONFLICT (id) DO NOTHING
        """, (
            DEFAULT_ANGRYCOIN_PRICE,
            time.time()
        ))

        conn.commit()

        logger.info("Database initialized.")

    except Exception:
        if conn:
            conn.rollback()

        logger.exception("Database initialization failed.")

        raise

    finally:
        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# USER HELPERS
# =========================================================

def get_user(user_id, name):
    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO users (
                user_id,
                name,
                coins,
                last_message
            )
            VALUES (%s, %s, 0, 0)

            ON CONFLICT (user_id)
            DO UPDATE SET name = EXCLUDED.name

            RETURNING coins, last_message
        """, (
            user_id,
            name
        ))

        row = cur.fetchone()

        conn.commit()

        return row[0], row[1]

    except Exception:
        if conn:
            conn.rollback()

        logger.exception("get_user failed.")
        raise

    finally:
        if cur:
            cur.close()

        release_db(conn)


def update_user(user_id, name, coins, last_message):
    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO users (
                user_id,
                name,
                coins,
                last_message
            )
            VALUES (%s, %s, %s, %s)

            ON CONFLICT (user_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                coins = EXCLUDED.coins,
                last_message = EXCLUDED.last_message
        """, (
            user_id,
            name,
            coins,
            last_message
        ))

        conn.commit()

    except Exception:
        if conn:
            conn.rollback()

        logger.exception("update_user failed.")
        raise

    finally:
        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# ADMIN CHECK
# =========================================================

def is_admin(user_id):
    return user_id == ADMIN_ID


# =========================================================
# START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return

    text = """
🤖 ربات کوین فعاله!

🪙 سیستم کوین:
• فولک = +10 🪙
• هاپهاپ کوین = +10 🪙
• /balance = موجودی
• /top = جدول کوین‌ها

🧠 اطلاعات عمومی:
• /quiz = سؤال جدید
• /quizscore = آمار Quiz
• /quiztop = رتبه‌بندی Quiz

📈 AngryCoin:
• /market = وضعیت بازار
• /buy تعداد = خرید
• /sell تعداد = فروش
• /portfolio = دارایی من

⏱️ هر گروه هر ۳۰ دقیقه فقط یک سؤال جدید می‌گیرد.

👑 دستورات ادمین:

/addcoins 1000
/removecoins 1000
/playerstats
/say متن
/groupmsg متن
/setgroup

📈 مدیریت AngryCoin:
/setmarketgroup
/unsetmarketgroup
/setprice 150

🧠 مدیریت سؤال:
/addquestion
/questions
/delquestion ID
/enablequestion ID
/disablequestion ID
"""

    await update.message.reply_text(text)


# =========================================================
# COIN MINING
# =========================================================

COIN_WORDS = {
    "فولک",
    "هاپهاپ کوین",
}


async def coin_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.message

    if not message:
        return

    if not message.text:
        return

    if not message.from_user:
        return

    text = message.text.strip()

    if text not in COIN_WORDS:
        return

    user = message.from_user

    user_id = user.id
    name = user.first_name or "کاربر"

    now = time.time()

    try:
        coins, last_message = get_user(
            user_id,
            name
        )

        if now - last_message < COOLDOWN:
            return

        coins += COINS_PER_MESSAGE

        update_user(
            user_id,
            name,
            coins,
            now
        )

        await message.reply_text(
            f"🪙 {name} +{COINS_PER_MESSAGE} کوین گرفت!"
        )

    except Exception:
        logger.exception("COIN ERROR")


# =========================================================
# BALANCE
# =========================================================

async def balance(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if not update.message:
        return

    user = update.effective_user

    name = user.first_name or "کاربر"

    try:
        coins, _ = get_user(
            user.id,
            name
        )

        await update.message.reply_text(
            f"💰 موجودی شما:\n\n"
            f"🪙 {coins} کوین"
        )

    except Exception:
        logger.exception("BALANCE ERROR")

        await update.message.reply_text(
            "❌ خطا در گرفتن موجودی."
        )


# =========================================================
# COIN TOP
# =========================================================

async def top(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT name, coins
            FROM users
            ORDER BY coins DESC
            LIMIT 10
        """)

        rows = cur.fetchall()

        if not rows:
            await update.message.reply_text(
                "🏆 هنوز کسی کوین نداره!"
            )
            return

        text = "🏆 جدول ۱۰ نفر برتر\n\n"

        medals = ["🥇", "🥈", "🥉"]

        for index, row in enumerate(rows, 1):

            name = row[0]
            coins = row[1]

            prefix = (
                medals[index - 1]
                if index <= 3
                else f"{index}."
            )

            text += (
                f"{prefix} "
                f"{name} — "
                f"{coins} 🪙\n"
            )

        await update.message.reply_text(text)

    except Exception:
        logger.exception("TOP ERROR")

        await update.message.reply_text(
            "❌ خطا در جدول."
        )

    finally:
        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# PLAYER STATS
# =========================================================

async def playerstats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message:
        return

    reply = update.message.reply_to_message

    if not reply:
        await update.message.reply_text(
            "❌ روی پیام شخص Reply کن."
        )
        return

    target = reply.from_user

    if not target:
        return

    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                name,
                coins
            FROM users
            WHERE user_id = %s
        """, (
            target.id,
        ))

        user = cur.fetchone()

        if not user:
            await update.message.reply_text(
                "❌ این کاربر هنوز ثبت نشده."
            )
            return

        cur.execute("""
            SELECT
                answered,
                correct,
                coins_earned
            FROM quiz_user_stats
            WHERE user_id = %s
        """, (
            target.id,
        ))

        quiz = cur.fetchone()

        if quiz:
            answered = quiz[0]
            correct = quiz[1]
            quiz_coins = quiz[2]
        else:
            answered = 0
            correct = 0
            quiz_coins = 0

        cur.execute("""
            SELECT amount
            FROM market_holdings
            WHERE user_id = %s
        """, (
            target.id,
        ))

        holding = cur.fetchone()

        angrycoin = holding[0] if holding else 0

        await update.message.reply_text(
            f"📊 آمار بازیکن\n\n"
            f"👤 {user[0]}\n"
            f"🆔 {target.id}\n"
            f"🪙 کوین: {user[1]}\n\n"
            f"🧠 Quiz:\n"
            f"• پاسخ‌ها: {answered}\n"
            f"• درست: {correct}\n"
            f"• درآمد Quiz: {quiz_coins} 🪙\n\n"
            f"📈 AngryCoin:\n"
            f"• سهام: {angrycoin}"
        )

    except Exception:
        logger.exception("PLAYER STATS ERROR")

        await update.message.reply_text(
            "❌ خطا در گرفتن آمار."
        )

    finally:
        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# ADD COINS
# =========================================================

async def addcoins(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message:
        return

    reply = update.message.reply_to_message

    if not reply:
        await update.message.reply_text(
            "❌ روی پیام شخص Reply کن."
        )
        return

    if not context.args:
        await update.message.reply_text(
            "مثال:\n/addcoins 1000"
        )
        return

    try:
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ مقدار باید عدد باشد."
        )
        return

    if amount <= 0:
        return

    target = reply.from_user

    if not target:
        return

    name = target.first_name or "کاربر"

    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO users (
                user_id,
                name,
                coins,
                last_message
            )
            VALUES (%s, %s, %s, 0)

            ON CONFLICT (user_id)
            DO UPDATE SET
                coins = users.coins + EXCLUDED.coins,
                name = EXCLUDED.name
        """, (
            target.id,
            name,
            amount
        ))

        conn.commit()

        await update.message.reply_text(
            f"✅ {amount} 🪙 به {name} اضافه شد."
        )

    except Exception:
        if conn:
            conn.rollback()

        logger.exception("ADD COINS ERROR")

        await update.message.reply_text(
            "❌ خطا."
        )

    finally:
        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# REMOVE COINS
# =========================================================

async def removecoins(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message:
        return

    reply = update.message.reply_to_message

    if not reply:
        await update.message.reply_text(
            "❌ روی پیام شخص Reply کن."
        )
        return

    if not context.args:
        await update.message.reply_text(
            "مثال:\n/removecoins 100"
        )
        return

    try:
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ مقدار باید عدد باشد."
        )
        return

    if amount <= 0:
        return

    target = reply.from_user

    if not target:
        return

    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            UPDATE users
            SET coins = GREATEST(coins - %s, 0)
            WHERE user_id = %s
        """, (
            amount,
            target.id
        ))

        conn.commit()

        await update.message.reply_text(
            f"✅ {amount} 🪙 از کاربر کم شد."
        )

    except Exception:
        if conn:
            conn.rollback()

        logger.exception("REMOVE COINS ERROR")

        await update.message.reply_text(
            "❌ خطا."
        )

    finally:
        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# SET GROUP
# =========================================================

async def setgroup(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        return

    if not update.effective_chat:
        return

    if not update.message:
        return

    chat = update.effective_chat

    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text(
            "❌ این دستور باید داخل گروه اجرا شود."
        )
        return

    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO bot_groups (
                chat_id,
                title
            )
            VALUES (%s, %s)

            ON CONFLICT (chat_id)
            DO UPDATE SET title = EXCLUDED.title
        """, (
            chat.id,
            chat.title or "گروه"
        ))

        conn.commit()

        await update.message.reply_text(
            "✅ این گروه ثبت شد."
        )

    except Exception:
        if conn:
            conn.rollback()

        logger.exception("SET GROUP ERROR")

        await update.message.reply_text(
            "❌ خطا."
        )

    finally:
        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# SAY
# =========================================================

async def say(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message:
        return

    text = " ".join(context.args).strip()

    if not text:
        await update.message.reply_text(
            "مثال:\n/say سلام بچه‌ها"
        )
        return

    await update.message.reply_text(text)


# =========================================================
# GROUP MESSAGE
# =========================================================

async def groupmsg(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message:
        return

    text = " ".join(context.args).strip()

    if not text:
        await update.message.reply_text(
            "مثال:\n/groupmsg سلام"
        )
        return

    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT chat_id
            FROM bot_groups
        """)

        groups = cur.fetchall()

        sent = 0

        for row in groups:
            chat_id = row[0]

            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=text
                )

                sent += 1

            except Exception:
                logger.exception(
                    "Could not send group message."
                )

        await update.message.reply_text(
            f"✅ پیام به {sent} گروه ارسال شد."
        )

    except Exception:
        logger.exception("GROUPMSG ERROR")

    finally:
        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# QUIZ QUESTIONS
# =========================================================

QUESTIONS = [

    (
        "پایتخت ایران کدام است؟",
        ["تهران", "تبریز", "اصفهان", "شیراز"],
        0
    ),

    (
        "بزرگ‌ترین سیاره منظومه شمسی کدام است؟",
        ["زمین", "مریخ", "مشتری", "زحل"],
        2
    ),

    (
        "آب در چند درجه سانتی‌گراد در فشار معمولی می‌جوشد؟",
        ["50", "75", "100", "150"],
        2
    ),

    (
        "کدام سیاره به سیاره سرخ معروف است؟",
        ["زهره", "مریخ", "مشتری", "عطارد"],
        1
    ),

    (
        "بزرگ‌ترین اقیانوس جهان کدام است؟",
        ["اطلس", "هند", "آرام", "منجمد شمالی"],
        2
    ),

    (
        "کدام حیوان سریع‌تر می‌دود؟",
        ["یوزپلنگ", "فیل", "اسب آبی", "خرس"],
        0
    ),

    (
        "واحد اندازه‌گیری جریان الکتریکی چیست؟",
        ["ولت", "آمپر", "وات", "اهم"],
        1
    ),

    (
        "کدام گاز بیشترین مقدار را در جو زمین دارد؟",
        ["اکسیژن", "دی‌اکسید کربن", "نیتروژن", "هیدروژن"],
        2
    ),

    (
        "تعداد قاره‌های زمین چند تاست؟",
        ["5", "6", "7", "8"],
        2
    ),

    (
        "کدام فلز نماد شیمیایی Fe دارد؟",
        ["طلا", "آهن", "نقره", "مس"],
        1
    ),

    (
        "اولین سیاره منظومه شمسی کدام است؟",
        ["عطارد", "زهره", "زمین", "مریخ"],
        0
    ),

    (
        "کدام اندام وظیفه پمپاژ خون را دارد؟",
        ["ریه", "کبد", "قلب", "کلیه"],
        2
    ),

    (
        "کدام کشور به سرزمین آفتاب تابان معروف است؟",
        ["چین", "ژاپن", "کره جنوبی", "هند"],
        1
    ),

    (
        "عدد اول بعد از 7 کدام است؟",
        ["8", "9", "10", "11"],
        3
    ),

    (
        "کدام پرنده نمی‌تواند پرواز کند؟",
        ["عقاب", "پنگوئن", "کبوتر", "شاهین"],
        1
    ),

    (
        "کدام سیاره نزدیک‌ترین سیاره به خورشید است؟",
        ["عطارد", "زمین", "مریخ", "زهره"],
        0
    ),

    (
        "نماد شیمیایی طلا چیست؟",
        ["Ag", "Au", "Fe", "Cu"],
        1
    ),

    (
        "کدام کشور بیشترین جمعیت را در آفریقا دارد؟",
        ["مصر", "نیجریه", "آفریقای جنوبی", "الجزایر"],
        1
    ),

    (
        "چند ضلع در یک شش‌ضلعی وجود دارد؟",
        ["5", "6", "7", "8"],
        1
    ),

    (
        "کدام جانور پستاندار است؟",
        ["مار", "قورباغه", "دلفین", "عقاب"],
        2
    ),

    (
        "بزرگ‌ترین قاره جهان کدام است؟",
        ["آفریقا", "اروپا", "آسیا", "آمریکای شمالی"],
        2
    ),

    (
        "کدام ماده برای تنفس انسان ضروری است؟",
        ["نیتروژن", "اکسیژن", "هلیوم", "متان"],
        1
    ),

    (
        "کدام کشور برج ایفل در آن قرار دارد؟",
        ["ایتالیا", "فرانسه", "آلمان", "اسپانیا"],
        1
    ),

    (
        "رنگ حاصل از ترکیب آبی و زرد چیست؟",
        ["سبز", "بنفش", "نارنجی", "قرمز"],
        0
    ),

    (
        "کدام عدد بر 2 بخش‌پذیر است؟",
        ["13", "17", "21", "24"],
        3
    ),

    (
        "کدام سیاره حلقه‌های معروف دارد؟",
        ["زمین", "زحل", "مریخ", "عطارد"],
        1
    ),

    (
        "کدام بخش گیاه آب را از خاک جذب می‌کند؟",
        ["گل", "برگ", "ریشه", "میوه"],
        2
    ),

    (
        "پایتخت ژاپن کدام است؟",
        ["کیوتو", "توکیو", "اوساکا", "هیروشیما"],
        1
    ),

    (
        "کدام حیوان به سلطان جنگل معروف است؟",
        ["ببر", "شیر", "پلنگ", "گرگ"],
        1
    ),

    (
        "چند دقیقه در یک ساعت وجود دارد؟",
        ["30", "45", "60", "90"],
        2
    ),

    (
        "کدام سیاره بزرگ‌ترین قمر منظومه شمسی را دارد؟",
        ["مشتری", "زمین", "مریخ", "زحل"],
        0
    ),

    (
        "کدام زبان بیشترین گویشور بومی را دارد؟",
        ["انگلیسی", "چینی ماندارین", "فرانسوی", "اسپانیایی"],
        1
    ),

    (
        "کدام کشور شکل چکمه دارد؟",
        ["فرانسه", "ایتالیا", "یونان", "پرتغال"],
        1
    ),

    (
        "خورشید یک چیست؟",
        ["سیاره", "ستاره", "قمر", "سیارک"],
        1
    ),

    (
        "کدام فلز در دمای اتاق مایع است؟",
        ["آهن", "جیوه", "مس", "آلومینیوم"],
        1
    ),

    (
        "کدام اقیانوس بین آفریقا و استرالیا قرار دارد؟",
        ["اطلس", "هند", "آرام", "شمالگان"],
        1
    ),

    (
        "کدام کشور اهرام جیزه را دارد؟",
        ["مصر", "عراق", "مکزیک", "یونان"],
        0
    ),

    (
        "کدام عضو بدن مسئول اصلی تصفیه خون است؟",
        ["قلب", "کلیه", "معده", "ریه"],
        1
    ),

    (
        "کدام عدد مربع کامل است؟",
        ["18", "20", "25", "30"],
        2
    ),

    (
        "کدام حیوان بزرگ‌ترین حیوان روی زمین است؟",
        ["فیل", "نهنگ آبی", "کوسه سفید", "زرافه"],
        1
    ),

    (
        "کدام سیاره بیشترین شباهت اندازه‌ای به زمین دارد؟",
        ["مریخ", "زهره", "مشتری", "نپتون"],
        1
    ),

    (
        "کدام کشور دیوار بزرگ معروف را دارد؟",
        ["چین", "هند", "ژاپن", "مغولستان"],
        0
    ),

    (
        "کدام عنصر نماد O دارد؟",
        ["طلا", "اکسیژن", "اوسمیم", "نقره"],
        1
    ),

    (
        "یک کیلومتر چند متر است؟",
        ["10", "100", "1000", "10000"],
        2
    ),

    (
        "کدام جانور دوزیست است؟",
        ["مار", "قورباغه", "گربه", "عقاب"],
        1
    ),

    (
        "پایتخت ایتالیا چیست؟",
        ["میلان", "ونیز", "رم", "ناپل"],
        2
    ),

    (
        "کدام سیاره به خورشید نزدیک‌تر است؟",
        ["زهره", "عطارد", "زمین", "مریخ"],
        1
    ),

    (
        "کدام وسیله برای اندازه‌گیری دما استفاده می‌شود؟",
        ["فشارسنج", "دماسنج", "ترازو", "ولت‌متر"],
        1
    ),

    (
        "کدام شکل سه ضلع دارد؟",
        ["مربع", "مثلث", "پنج‌ضلعی", "دایره"],
        1
    ),

    (
        "کدام گاز برای فتوسنتز گیاهان استفاده می‌شود؟",
        ["اکسیژن", "هیدروژن", "دی‌اکسید کربن", "نیتروژن"],
        2
    ),

    (
        "کدام کشور به سرزمین هزار دریاچه معروف است؟",
        ["فنلاند", "نروژ", "سوئد", "دانمارک"],
        0
    ),

    (
        "کدام سیاره دارای بیشترین دمای سطحی در منظومه شمسی است؟",
        ["عطارد", "زهره", "مریخ", "مشتری"],
        1
    ),

    (
        "کدام بخش سلول مرکز کنترل فعالیت‌های سلول است؟",
        ["هسته", "غشا", "سیتوپلاسم", "ریبوزوم"],
        0
    ),

    (
        "کدام کشور فوتبال را در المپیک مدرن اولیه بیشتر توسعه داد؟",
        ["برزیل", "انگلیس", "آمریکا", "ژاپن"],
        1
    ),

    (
        "کدام قمر متعلق به زمین است؟",
        ["تایتان", "ماه", "اروپا", "گانیمد"],
        1
    ),

    (
        "کدام عدد اول است؟",
        ["21", "27", "29", "33"],
        2
    ),

    (
        "کدام ماده در دمای اتاق جامد است؟",
        ["اکسیژن", "آهن", "هلیوم", "نیتروژن"],
        1
    ),

    (
        "کدام کشور پایتختش برلین است؟",
        ["اتریش", "آلمان", "بلژیک", "هلند"],
        1
    ),

    (
        "کدام دستگاه برای مشاهده اجرام آسمانی استفاده می‌شود؟",
        ["میکروسکوپ", "تلسکوپ", "بارومتر", "دماسنج"],
        1
    ),

    (
        "کدام اقیانوس بزرگ‌ترین اقیانوس جهان است؟",
        ["هند", "آرام", "اطلس", "قطب شمال"],
        1
    ),

    (
        "کدام ماده باعث قرمز شدن خون می‌شود؟",
        ["هموگلوبین", "کلروفیل", "کراتین", "کلاژن"],
        0
    ),

    (
        "کدام کشور پایتختش مادرید است؟",
        ["پرتغال", "اسپانیا", "ایتالیا", "فرانسه"],
        1
    ),

    (
        "کدام سیاره دارای لکه قرمز بزرگ معروف است؟",
        ["مریخ", "مشتری", "زحل", "نپتون"],
        1
    ),

    (
        "کدام ماده در گیاهان سبز نور را جذب می‌کند؟",
        ["هموگلوبین", "کلروفیل", "نشاسته", "گلوکز"],
        1
    ),

    (
        "کدام کشور پایتختش مسکو است؟",
        ["اوکراین", "روسیه", "بلاروس", "لهستان"],
        1
    ),

    (
        "کدام اندام اکسیژن را وارد خون می‌کند؟",
        ["قلب", "ریه", "کبد", "معده"],
        1
    ),

    (
        "کدام عدد حاصل 9×9 است؟",
        ["72", "81", "90", "99"],
        1
    ),

    (
        "کدام سیاره دورترین سیاره شناخته‌شده منظومه شمسی است؟",
        ["اورانوس", "نپتون", "زحل", "مریخ"],
        1
    ),

    (
        "کدام کشور پایتختش کانبرا است؟",
        ["استرالیا", "کانادا", "نیوزیلند", "آفریقای جنوبی"],
        0
    ),

    (
        "کدام حیوان گردن بسیار بلندی دارد؟",
        ["زرافه", "کرگدن", "فیل", "گورخر"],
        0
    ),

    (
        "کدام واحد برای اندازه‌گیری انرژی الکتریکی رایج است؟",
        ["وات", "کیلووات‌ساعت", "ولت", "آمپر"],
        1
    ),

    (
        "کدام کشور پایتختش اتاوا است؟",
        ["آمریکا", "کانادا", "مکزیک", "برزیل"],
        1
    ),

    (
        "کدام عنصر برای تنفس ضروری است؟",
        ["اکسیژن", "طلا", "آهن", "مس"],
        0
    ),

    (
        "کدام شکل هیچ ضلعی ندارد؟",
        ["مثلث", "مربع", "دایره", "مستطیل"],
        2
    ),

    (
        "کدام قاره سردترین قاره جهان است؟",
        ["آسیا", "اروپا", "جنوبگان", "آفریقا"],
        2
    ),

    (
        "کدام سیاره معروف به سیاره آبی است؟",
        ["زمین", "مریخ", "زهره", "زحل"],
        0
    ),

    (
        "کدام کشور پایتختش سئول است؟",
        ["کره جنوبی", "چین", "ژاپن", "ویتنام"],
        0
    ),

    (
        "کدام جانور معمولاً با هشت پا شناخته می‌شود؟",
        ["اختاپوس", "خرچنگ", "دلفین", "کوسه"],
        0
    ),

    (
        "کدام عدد بر 5 بخش‌پذیر است؟",
        ["17", "23", "35", "42"],
        2
    ),

    (
        "کدام ماده غذایی منبع مهم کلسیم است؟",
        ["شیر", "برنج", "سیب", "روغن"],
        0
    ),

    (
        "کدام کشور پایتختش آنکارا است؟",
        ["ترکیه", "ایران", "گرجستان", "ارمنستان"],
        0
    ),

    (
        "کدام نیرو ما را به سمت زمین می‌کشد؟",
        ["اصطکاک", "گرانش", "مغناطیس", "الکتریسیته"],
        1
    ),

    (
        "کدام سیاره به دلیل حلقه‌هایش معروف است؟",
        ["مریخ", "زحل", "زمین", "زهره"],
        1
    ),

    (
        "کدام اندام غذا را هضم می‌کند؟",
        ["معده", "ریه", "قلب", "مغز"],
        0
    ),

    (
        "کدام کشور پایتختش واشنگتن دی‌سی است؟",
        ["کانادا", "آمریکا", "مکزیک", "برزیل"],
        1
    ),

    (
        "کدام فلز نماد Ag دارد؟",
        ["طلا", "نقره", "آهن", "مس"],
        1
    ),

    (
        "کدام عدد حاصل 12×12 است؟",
        ["124", "134", "144", "154"],
        2
    ),

    (
        "کدام سیاره دارای قمر فوبوس است؟",
        ["زمین", "مریخ", "زحل", "زهره"],
        1
    ),

    (
        "کدام کشور پایتختش پکن است؟",
        ["چین", "ژاپن", "کره جنوبی", "تایلند"],
        0
    ),

    (
        "کدام اندام مرکز اصلی دستگاه عصبی است؟",
        ["مغز", "قلب", "کبد", "ریه"],
        0
    ),

    (
        "کدام گاز برای سوختن لازم است؟",
        ["اکسیژن", "نیتروژن", "هلیوم", "آرگون"],
        0
    ),

    (
        "کدام قاره بیشترین وسعت را دارد؟",
        ["آفریقا", "آسیا", "اروپا", "استرالیا"],
        1
    ),

    (
        "کدام کشور پایتختش لیسبون است؟",
        ["اسپانیا", "پرتغال", "ایتالیا", "یونان"],
        1
    ),

    (
        "کدام وسیله وزن را اندازه می‌گیرد؟",
        ["ترازو", "دماسنج", "خط‌کش", "ساعت"],
        0
    ),

    (
        "کدام عدد حاصل 15+25 است؟",
        ["30", "35", "40", "45"],
        2
    ),

    (
        "کدام سیاره دارای قمر تایتان است؟",
        ["زحل", "مریخ", "زمین", "زهره"],
        0
    ),

    (
        "کدام کشور پایتختش دهلی نو است؟",
        ["پاکستان", "هند", "نپال", "بنگلادش"],
        1
    ),

    (
        "کدام بخش گیاه معمولاً فتوسنتز می‌کند؟",
        ["ریشه", "برگ", "دانه", "ریشه‌چه"],
        1
    ),

    (
        "کدام عدد اول است؟",
        ["15", "19", "21", "25"],
        1
    ),

    (
        "کدام حیوان بزرگ‌ترین پرنده زنده جهان است؟",
        ["عقاب", "شترمرغ", "پنگوئن", "کبوتر"],
        1
    ),

    (
        "کدام کشور پایتختش ریاض است؟",
        ["عربستان سعودی", "عراق", "اردن", "عمان"],
        0
    ),

    (
        "کدام فلز رسانای بسیار خوب برق است؟",
        ["چوب", "مس", "شیشه", "پلاستیک"],
        1
    ),

    (
        "کدام سیاره دارای روز بسیار طولانی است؟",
        ["زهره", "مریخ", "زمین", "نپتون"],
        0
    ),

    (
        "کدام کشور پایتختش آتن است؟",
        ["یونان", "ایتالیا", "ترکیه", "قبرس"],
        0
    ),

]


# =========================================================
# LOAD DEFAULT QUESTIONS
# =========================================================

def load_default_questions():

    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT COUNT(*)
            FROM quiz_questions
        """)

        count = cur.fetchone()[0]

        if count > 0:
            conn.rollback()
            return

        for question, options, correct in QUESTIONS:

            cur.execute("""
                INSERT INTO quiz_questions (
                    question,
                    option1,
                    option2,
                    option3,
                    option4,
                    correct_option,
                    enabled,
                    created_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    TRUE,
                    %s
                )
            """, (
                question,
                options[0],
                options[1],
                options[2],
                options[3],
                correct,
                time.time()
            ))

        conn.commit()

        logger.info(
            "Loaded %s default quiz questions.",
            len(QUESTIONS)
        )

    except Exception:
        if conn:
            conn.rollback()

        logger.exception(
            "Could not load default questions."
        )

    finally:
        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# QUIZ COOLDOWN
# =========================================================

def can_start_quiz(chat_id):

    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        now = time.time()

        cur.execute("""
            SELECT last_question_at
            FROM quiz_cooldowns
            WHERE chat_id = %s
            FOR UPDATE
        """, (
            chat_id,
        ))

        row = cur.fetchone()

        if row:
            last = row[0]

            if now - last < QUIZ_COOLDOWN:

                remaining = (
                    QUIZ_COOLDOWN -
                    (now - last)
                )

                conn.rollback()

                return False, remaining

            cur.execute("""
                UPDATE quiz_cooldowns
                SET last_question_at = %s
                WHERE chat_id = %s
            """, (
                now,
                chat_id
            ))

        else:

            cur.execute("""
                INSERT INTO quiz_cooldowns (
                    chat_id,
                    last_question_at
                )
                VALUES (%s, %s)
            """, (
                chat_id,
                now
            ))

        conn.commit()

        return True, 0

    except Exception:
        if conn:
            conn.rollback()

        logger.exception(
            "QUIZ COOLDOWN ERROR"
        )

        return False, 0

    finally:
        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# GET RANDOM QUESTION
# =========================================================

def get_random_question():

    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                id,
                question,
                option1,
                option2,
                option3,
                option4,
                correct_option
            FROM quiz_questions
            WHERE enabled = TRUE
            ORDER BY RANDOM()
            LIMIT 1
        """)

        row = cur.fetchone()

        conn.rollback()

        return row

    except Exception:
        if conn:
            conn.rollback()

        logger.exception(
            "GET QUESTION ERROR"
        )

        return None

    finally:
        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# QUIZ COMMAND
# =========================================================

async def quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.effective_chat:
        return

    chat = update.effective_chat

    if chat.type not in (
        "group",
        "supergroup"
    ):

        await update.message.reply_text(
            "❌ Quiz فقط داخل گروه قابل استفاده است."
        )

        return

    allowed, remaining = can_start_quiz(
        chat.id
    )

    if not allowed:

        minutes = int(
            remaining // 60
        ) + 1

        await update.message.reply_text(
            f"⏳ سؤال بعدی حدود "
            f"{minutes} دقیقه دیگر قابل نمایش است."
        )

        return

    question = get_random_question()

    if not question:

        await update.message.reply_text(
            "❌ فعلاً سؤالی وجود ندارد."
        )

        return

    (
        question_id,
        question_text,
        option1,
        option2,
        option3,
        option4,
        correct_option
    ) = question

    options = [
        option1,
        option2,
        option3,
        option4
    ]

    try:

        sent = await context.bot.send_poll(
            chat_id=chat.id,
            question=question_text,
            options=options,
            type=Poll.QUIZ,
            correct_option_id=correct_option,
            is_anonymous=False,
        )

        poll_id = sent.poll.id

        conn = None
        cur = None

        try:
            conn = get_db()
            cur = conn.cursor()

            cur.execute("""
                INSERT INTO quiz_polls (
                    poll_id,
                    question_id,
                    chat_id,
                    message_id,
                    created_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
            """, (
                poll_id,
                question_id,
                chat.id,
                sent.message_id,
                time.time()
            ))

            conn.commit()

        except Exception:
            if conn:
                conn.rollback()

            logger.exception(
                "QUIZ POLL SAVE ERROR"
            )

        finally:
            if cur:
                cur.close()

            release_db(conn)

    except Exception:
        logger.exception("QUIZ SEND ERROR")

        await update.message.reply_text(
            "❌ نتونستم سؤال رو ارسال کنم."
        )


# =========================================================
# QUIZ POLL ANSWER
# =========================================================

async def quiz_answer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    answer = update.poll_answer

    if not answer:
        return

    poll_id = answer.poll_id

    user = answer.user

    selected = (
        answer.option_ids[0]
        if answer.option_ids
        else None
    )

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        # -----------------------------------------
        # FIND POLL
        # -----------------------------------------

        cur.execute("""
            SELECT question_id
            FROM quiz_polls
            WHERE poll_id = %s
        """, (
            poll_id,
        ))

        poll = cur.fetchone()

        if not poll:
            conn.rollback()
            return

        question_id = poll[0]

        # -----------------------------------------
        # PREVENT DOUBLE REWARD
        # -----------------------------------------

        cur.execute("""
            SELECT 1
            FROM quiz_answers
            WHERE poll_id = %s
              AND user_id = %s
        """, (
            poll_id,
            user.id
        ))

        if cur.fetchone():

            conn.rollback()
            return

        # -----------------------------------------
        # GET CORRECT ANSWER
        # -----------------------------------------

        cur.execute("""
            SELECT correct_option
            FROM quiz_questions
            WHERE id = %s
        """, (
            question_id,
        ))

        row = cur.fetchone()

        if not row:

            conn.rollback()
            return

        correct_option = row[0]

        is_correct = (
            selected is not None
            and selected == correct_option
        )

        reward = (
            QUIZ_REWARD
            if is_correct
            else 0
        )

        # -----------------------------------------
        # SAVE ANSWER
        # -----------------------------------------

        cur.execute("""
            INSERT INTO quiz_answers (
                poll_id,
                user_id,
                selected_option,
                is_correct,
                coins_awarded,
                answered_at
            )
            VALUES (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s
            )
        """, (
            poll_id,
            user.id,
            selected,
            is_correct,
            reward,
            time.time()
        ))

        # -----------------------------------------
        # UPDATE USER
        # -----------------------------------------

        name = user.first_name or "کاربر"

        cur.execute("""
            INSERT INTO users (
                user_id,
                name,
                coins,
                last_message
            )
            VALUES (
                %s,
                %s,
                %s,
                0
            )

            ON CONFLICT (user_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                coins = users.coins + EXCLUDED.coins
        """, (
            user.id,
            name,
            reward
        ))

        # -----------------------------------------
        # UPDATE QUIZ STATS
        # -----------------------------------------

        cur.execute("""
            INSERT INTO quiz_user_stats (
                user_id,
                answered,
                correct,
                coins_earned
            )
            VALUES (
                %s,
                1,
                %s,
                %s
            )

            ON CONFLICT (user_id)
            DO UPDATE SET
                answered =
                    quiz_user_stats.answered + 1,

                correct =
                    quiz_user_stats.correct + EXCLUDED.correct,

                coins_earned =
                    quiz_user_stats.coins_earned
                    + EXCLUDED.coins_earned
        """, (
            user.id,
            1 if is_correct else 0,
            reward
        ))

        conn.commit()

        # -----------------------------------------
        # PRIVATE RESULT
        # -----------------------------------------

        if is_correct:

            try:
                await context.bot.send_message(
                    chat_id=user.id,
                    text=(
                        "🎉 درست جواب دادی!\n\n"
                        f"🪙 +{QUIZ_REWARD} کوین"
                    )
                )

            except Exception:
                pass

        else:

            try:
                await context.bot.send_message(
                    chat_id=user.id,
                    text="❌ جواب درست نبود!"
                )

            except Exception:
                pass

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "QUIZ ANSWER ERROR"
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# QUIZ SCORE
# =========================================================

async def quizscore(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if not update.message:
        return

    user = update.effective_user

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                answered,
                correct,
                coins_earned
            FROM quiz_user_stats
            WHERE user_id = %s
        """, (
            user.id,
        ))

        row = cur.fetchone()

        if not row:

            await update.message.reply_text(
                "🧠 هنوز در هیچ Quizای شرکت نکردی."
            )

            return

        answered = row[0]
        correct = row[1]
        coins = row[2]

        await update.message.reply_text(
            f"🧠 آمار Quiz\n\n"
            f"📝 پاسخ داده‌شده: {answered}\n"
            f"✅ جواب درست: {correct}\n"
            f"🪙 درآمد: {coins} کوین"
        )

    except Exception:
        logger.exception(
            "QUIZ SCORE ERROR"
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# QUIZ TOP
# =========================================================

async def quiztop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                u.name,
                s.correct,
                s.coins_earned
            FROM quiz_user_stats s
            JOIN users u
              ON u.user_id = s.user_id
            ORDER BY
                s.correct DESC,
                s.coins_earned DESC
            LIMIT 10
        """)

        rows = cur.fetchall()

        if not rows:

            await update.message.reply_text(
                "🧠 هنوز کسی Quiz بازی نکرده."
            )

            return

        text = "🧠 جدول Quiz\n\n"

        medals = [
            "🥇",
            "🥈",
            "🥉"
        ]

        for index, row in enumerate(
            rows,
            1
        ):

            name = row[0]
            correct = row[1]
            coins = row[2]

            prefix = (
                medals[index - 1]
                if index <= 3
                else f"{index}."
            )

            text += (
                f"{prefix} {name}\n"
                f"   ✅ {correct} درست"
                f" | 🪙 {coins}\n"
            )

        await update.message.reply_text(
            text
        )

    except Exception:
        logger.exception(
            "QUIZ TOP ERROR"
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# ADD QUESTION
# =========================================================

async def addquestion(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message:
        return

    text = update.message.text

    parts = text.split("|")

    if len(parts) != 6:

        await update.message.reply_text(
            "❌ فرمت اشتباهه.\n\n"
            "فرمت:\n"
            "/addquestion سوال | گزینه۱ | گزینه۲ | گزینه۳ | گزینه۴ | جواب\n\n"
            "جواب باید عدد 1 تا 4 باشد.\n\n"
            "مثال:\n"
            "/addquestion پایتخت ایران؟ | تهران | تبریز | شیراز | قم | 1"
        )

        return

    question = parts[0].replace(
        "/addquestion",
        "",
        1
    ).strip()

    options = [
        parts[1].strip(),
        parts[2].strip(),
        parts[3].strip(),
        parts[4].strip()
    ]

    try:
        correct = int(
            parts[5].strip()
        ) - 1

    except ValueError:

        await update.message.reply_text(
            "❌ جواب باید عدد 1 تا 4 باشد."
        )

        return

    if not question:
        return

    if any(
        not option
        for option in options
    ):

        await update.message.reply_text(
            "❌ همه گزینه‌ها باید پر باشند."
        )

        return

    if correct not in range(4):

        await update.message.reply_text(
            "❌ جواب باید بین 1 تا 4 باشد."
        )

        return

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO quiz_questions (
                question,
                option1,
                option2,
                option3,
                option4,
                correct_option,
                enabled,
                created_at
            )
            VALUES (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                TRUE,
                %s
            )
            RETURNING id
        """, (
            question,
            options[0],
            options[1],
            options[2],
            options[3],
            correct,
            time.time()
        ))

        question_id = cur.fetchone()[0]

        conn.commit()

        await update.message.reply_text(
            f"✅ سؤال با موفقیت اضافه شد.\n"
            f"🆔 ID: {question_id}"
        )

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "ADD QUESTION ERROR"
        )

        await update.message.reply_text(
            "❌ خطا در اضافه کردن سؤال."
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# QUESTIONS
# =========================================================

async def questions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message:
        return

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                id,
                question,
                enabled
            FROM quiz_questions
            ORDER BY id DESC
            LIMIT 30
        """)

        rows = cur.fetchall()

        if not rows:

            await update.message.reply_text(
                "❌ هیچ سؤالی وجود ندارد."
            )

            return

        text = "🧠 لیست سؤال‌ها\n\n"

        for row in rows:

            status = (
                "🟢"
                if row[2]
                else "🔴"
            )

            text += (
                f"{status} #{row[0]}\n"
                f"{row[1]}\n\n"
            )

        await update.message.reply_text(
            text[:4000]
        )

    except Exception:
        logger.exception(
            "QUESTIONS ERROR"
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# DELETE QUESTION
# =========================================================

async def delquestion(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message:
        return

    if not context.args:

        await update.message.reply_text(
            "مثال:\n/delquestion 5"
        )

        return

    try:
        question_id = int(
            context.args[0]
        )
    except ValueError:

        await update.message.reply_text(
            "❌ ID نامعتبر."
        )

        return

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            DELETE FROM quiz_questions
            WHERE id = %s
        """, (
            question_id,
        ))

        deleted = cur.rowcount

        conn.commit()

        if deleted:

            await update.message.reply_text(
                "✅ سؤال حذف شد."
            )

        else:

            await update.message.reply_text(
                "❌ چنین سؤالی پیدا نشد."
            )

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "DELETE QUESTION ERROR"
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# ENABLE / DISABLE QUESTION
# =========================================================

async def set_question_enabled(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    enabled: bool
):

    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message:
        return

    if not context.args:

        await update.message.reply_text(
            "مثال:\n"
            "/enablequestion 5"
        )

        return

    try:
        question_id = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ ID نامعتبر."
        )

        return

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            UPDATE quiz_questions
            SET enabled = %s
            WHERE id = %s
        """, (
            enabled,
            question_id
        ))

        changed = cur.rowcount

        conn.commit()

        if changed:

            await update.message.reply_text(
                "✅ انجام شد."
            )

        else:

            await update.message.reply_text(
                "❌ سؤال پیدا نشد."
            )

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "QUESTION ENABLE ERROR"
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)


async def enablequestion(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await set_question_enabled(
        update,
        context,
        True
    )


async def disablequestion(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await set_question_enabled(
        update,
        context,
        False
    )


# =========================================================
# MARKET HELPERS
# =========================================================

def get_market_price():

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                name,
                symbol,
                price
            FROM market
            WHERE id = 1
        """)

        row = cur.fetchone()

        conn.rollback()

        return row

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "MARKET PRICE ERROR"
        )

        return None

    finally:

        if cur:
            cur.close()

        release_db(conn)


def market_group_enabled(chat_id):

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT enabled
            FROM market_groups
            WHERE chat_id = %s
        """, (
            chat_id,
        ))

        row = cur.fetchone()

        conn.rollback()

        return bool(row and row[0])

    except Exception:

        if conn:
            conn.rollback()

        return False

    finally:

        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# MARKET
# =========================================================

async def market(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.effective_chat:
        return

    chat = update.effective_chat

    if chat.type in (
        "group",
        "supergroup"
    ):

        if not market_group_enabled(
            chat.id
        ):

            await update.message.reply_text(
                "📈 بازار AngryCoin در این گروه فعال نیست."
            )

            return

    data = get_market_price()

    if not data:

        await update.message.reply_text(
            "❌ بازار در دسترس نیست."
        )

        return

    name = data[0]
    symbol = data[1]
    price = data[2]

    user = update.effective_user

    coins = 0
    holding = 0

    if user:

        user_name = user.first_name or "کاربر"

        conn = None
        cur = None

        try:

            conn = get_db()
            cur = conn.cursor()

            cur.execute("""
                SELECT coins
                FROM users
                WHERE user_id = %s
            """, (
                user.id,
            ))

            row = cur.fetchone()

            if row:
                coins = row[0]

            cur.execute("""
                SELECT amount
                FROM market_holdings
                WHERE user_id = %s
            """, (
                user.id,
            ))

            row = cur.fetchone()

            if row:
                holding = row[0]

            conn.rollback()

        finally:

            if cur:
                cur.close()

            release_db(conn)

    await update.message.reply_text(
        f"📈 بازار {name}\n\n"
        f"🔹 نماد: {symbol}\n"
        f"💵 قیمت هر سهم: {price} 🪙\n\n"
        f"💰 موجودی: {coins} 🪙\n"
        f"📦 سهام شما: {holding}\n\n"
        f"خرید:\n"
        f"/buy 2\n\n"
        f"فروش:\n"
        f"/sell 2"
    )


# =========================================================
# BUY
# =========================================================

async def buy(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.effective_chat:
        return

    if update.effective_chat.type in (
        "group",
        "supergroup"
    ):

        if not market_group_enabled(
            update.effective_chat.id
        ):

            await update.message.reply_text(
                "❌ بازار در این گروه فعال نیست."
            )

            return

    if not update.effective_user:
        return

    if not context.args:

        await update.message.reply_text(
            "مثال:\n/buy 5"
        )

        return

    try:

        amount = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ تعداد باید عدد باشد."
        )

        return

    if amount <= 0:

        await update.message.reply_text(
            "❌ تعداد باید بیشتر از صفر باشد."
        )

        return

    user = update.effective_user

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        # Lock user
        cur.execute("""
            SELECT coins
            FROM users
            WHERE user_id = %s
            FOR UPDATE
        """, (
            user.id,
        ))

        user_row = cur.fetchone()

        if not user_row:

            cur.execute("""
                INSERT INTO users (
                    user_id,
                    name,
                    coins,
                    last_message
                )
                VALUES (%s, %s, 0, 0)
            """, (
                user.id,
                user.first_name or "کاربر"
            ))

            coins = 0

        else:

            coins = user_row[0]

        # Lock price
        cur.execute("""
            SELECT price
            FROM market
            WHERE id = 1
            FOR UPDATE
        """)

        market_row = cur.fetchone()

        if not market_row:

            conn.rollback()

            await update.message.reply_text(
                "❌ بازار وجود ندارد."
            )

            return

        price = float(
            market_row[0]
        )

        total = price * amount

        if coins < total:

            conn.rollback()

            await update.message.reply_text(
                f"❌ کوین کافی نداری.\n\n"
                f"💵 قیمت: {total:.2f} 🪙\n"
                f"💰 موجودی: {coins} 🪙"
            )

            return

        # Deduct coins
        cur.execute("""
            UPDATE users
            SET coins = coins - %s
            WHERE user_id = %s
        """, (
            total,
            user.id
        ))

        # Add holding
        cur.execute("""
            INSERT INTO market_holdings (
                user_id,
                amount
            )
            VALUES (
                %s,
                %s
            )

            ON CONFLICT (user_id)
            DO UPDATE SET
                amount =
                    market_holdings.amount
                    + EXCLUDED.amount
        """, (
            user.id,
            amount
        ))

        conn.commit()

        await update.message.reply_text(
            f"✅ خرید انجام شد!\n\n"
            f"📈 AngryCoin: {amount}\n"
            f"💵 قیمت: {price:.2f}\n"
            f"💰 هزینه: {total:.2f} 🪙"
        )

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "BUY ERROR"
        )

        await update.message.reply_text(
            "❌ خطا در خرید."
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# SELL
# =========================================================

async def sell(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.effective_chat:
        return

    if update.effective_chat.type in (
        "group",
        "supergroup"
    ):

        if not market_group_enabled(
            update.effective_chat.id
        ):

            await update.message.reply_text(
                "❌ بازار در این گروه فعال نیست."
            )

            return

    if not update.effective_user:
        return

    if not context.args:

        await update.message.reply_text(
            "مثال:\n/sell 5"
        )

        return

    try:

        amount = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ تعداد باید عدد باشد."
        )

        return

    if amount <= 0:

        await update.message.reply_text(
            "❌ تعداد باید بیشتر از صفر باشد."
        )

        return

    user = update.effective_user

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT amount
            FROM market_holdings
            WHERE user_id = %s
            FOR UPDATE
        """, (
            user.id,
        ))

        holding = cur.fetchone()

        if not holding:

            conn.rollback()

            await update.message.reply_text(
                "❌ هیچ AngryCoinای نداری."
            )

            return

        current_amount = float(
            holding[0]
        )

        if current_amount < amount:

            conn.rollback()

            await update.message.reply_text(
                f"❌ فقط {current_amount:g} سهم داری."
            )

            return

        cur.execute("""
            SELECT price
            FROM market
            WHERE id = 1
            FOR UPDATE
        """)

        market_row = cur.fetchone()

        if not market_row:

            conn.rollback()

            return

        price = float(
            market_row[0]
        )

        total = price * amount

        # Remove shares
        cur.execute("""
            UPDATE market_holdings
            SET amount = amount - %s
            WHERE user_id = %s
        """, (
            amount,
            user.id
        ))

        # Add coins
        cur.execute("""
            UPDATE users
            SET coins = coins + %s
            WHERE user_id = %s
        """, (
            total,
            user.id
        ))

        conn.commit()

        await update.message.reply_text(
            f"✅ فروش انجام شد!\n\n"
            f"📈 AngryCoin: {amount}\n"
            f"💵 قیمت: {price:.2f}\n"
            f"💰 دریافتی: {total:.2f} 🪙"
        )

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "SELL ERROR"
        )

        await update.message.reply_text(
            "❌ خطا در فروش."
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# PORTFOLIO
# =========================================================

async def portfolio(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.effective_user:
        return

    user = update.effective_user

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT coins
            FROM users
            WHERE user_id = %s
        """, (
            user.id,
        ))

        row = cur.fetchone()

        coins = row[0] if row else 0

        cur.execute("""
            SELECT amount
            FROM market_holdings
            WHERE user_id = %s
        """, (
            user.id,
        ))

        row = cur.fetchone()

        shares = (
            float(row[0])
            if row
            else 0
        )

        cur.execute("""
            SELECT price
            FROM market
            WHERE id = 1
        """)

        row = cur.fetchone()

        price = (
            float(row[0])
            if row
            else 0
        )

        value = shares * price

        conn.rollback()

        await update.message.reply_text(
            f"📊 پرتفوی شما\n\n"
            f"💰 کوین نقد: {coins} 🪙\n"
            f"📈 AngryCoin: {shares:g}\n"
            f"💵 ارزش سهام: {value:.2f} 🪙\n"
            f"📦 ارزش کل تقریبی: "
            f"{coins + value:.2f} 🪙"
        )

    except Exception:

        logger.exception(
            "PORTFOLIO ERROR"
        )

        await update.message.reply_text(
            "❌ خطا."
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# SET MARKET GROUP
# =========================================================

async def setmarketgroup(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        return

    if not update.effective_chat:
        return

    if not update.message:
        return

    chat = update.effective_chat

    if chat.type not in (
        "group",
        "supergroup"
    ):

        await update.message.reply_text(
            "❌ این دستور باید داخل گروه باشد."
        )

        return

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO market_groups (
                chat_id,
                title,
                enabled
            )
            VALUES (
                %s,
                %s,
                TRUE
            )

            ON CONFLICT (chat_id)
            DO UPDATE SET
                title = EXCLUDED.title,
                enabled = TRUE
        """, (
            chat.id,
            chat.title or "گروه"
        ))

        conn.commit()

        await update.message.reply_text(
            "📈 بازار AngryCoin در این گروه فعال شد."
        )

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "SET MARKET GROUP ERROR"
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# UNSET MARKET GROUP
# =========================================================

async def unsetmarketgroup(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        return

    if not update.effective_chat:
        return

    if not update.message:
        return

    chat = update.effective_chat

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            UPDATE market_groups
            SET enabled = FALSE
            WHERE chat_id = %s
        """, (
            chat.id,
        ))

        conn.commit()

        await update.message.reply_text(
            "📉 بازار AngryCoin در این گروه غیرفعال شد."
        )

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "UNSET MARKET GROUP ERROR"
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# SET PRICE
# =========================================================

async def setprice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message:
        return

    if not context.args:

        await update.message.reply_text(
            "مثال:\n/setprice 150"
        )

        return

    try:

        price = float(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ قیمت نامعتبر."
        )

        return

    if price <= 0:

        await update.message.reply_text(
            "❌ قیمت باید بیشتر از صفر باشد."
        )

        return

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        now = time.time()

        cur.execute("""
            UPDATE market
            SET
                price = %s,
                updated_at = %s
            WHERE id = 1
        """, (
            price,
            now
        ))

        cur.execute("""
            INSERT INTO market_history (
                price,
                created_at
            )
            VALUES (
                %s,
                %s
            )
        """, (
            price,
            now
        ))

        conn.commit()

        await update.message.reply_text(
            f"✅ قیمت AngryCoin تغییر کرد.\n\n"
            f"💵 قیمت جدید: {price:g} 🪙"
        )

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "SET PRICE ERROR"
        )

        await update.message.reply_text(
            "❌ خطا در تغییر قیمت."
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# FLASK
# =========================================================

web = Flask(__name__)


@web.route("/")
def home():

    return jsonify({
        "status": "online",
        "bot": "coin bot",
        "market": "AngryCoin"
    })


@web.route("/health")
def health():

    return jsonify({
        "status": "ok"
    })


# =========================================================
# FLASK SERVER
# =========================================================

def run_flask():

    web.run(
        host="0.0.0.0",
        port=PORT,
        use_reloader=False
    )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update,
    context
):

    logger.exception(
        "Telegram error:",
        exc_info=context.error
    )


# =========================================================
# MAIN
# =========================================================

def main():

    logger.info(
        "Starting bot..."
    )

    create_db_pool()

    init_db()

    load_default_questions()

    # -----------------------------------------
    # FLASK
    # -----------------------------------------

    flask_thread = Thread(
        target=run_flask,
        daemon=True
    )

    flask_thread.start()

    # -----------------------------------------
    # TELEGRAM
    # -----------------------------------------

    application = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    # -----------------------------------------
    # BASIC
    # -----------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "balance",
            balance
        )
    )

    application.add_handler(
        CommandHandler(
            "top",
            top
        )
    )

    # -----------------------------------------
    # ADMIN
    # -----------------------------------------

    application.add_handler(
        CommandHandler(
            "addcoins",
            addcoins
        )
    )

    application.add_handler(
        CommandHandler(
            "removecoins",
            removecoins
        )
    )

    application.add_handler(
        CommandHandler(
            "playerstats",
            playerstats
        )
    )

    application.add_handler(
        CommandHandler(
            "say",
            say
        )
    )

    application.add_handler(
        CommandHandler(
            "groupmsg",
            groupmsg
        )
    )

    application.add_handler(
        CommandHandler(
            "setgroup",
            setgroup
        )
    )

    # -----------------------------------------
    # QUIZ
    # -----------------------------------------

    application.add_handler(
        CommandHandler(
            "quiz",
            quiz
        )
    )

    application.add_handler(
        CommandHandler(
            "quizscore",
            quizscore
        )
    )

    application.add_handler(
        CommandHandler(
            "quiztop",
            quiztop
        )
    )

    application.add_handler(
        CommandHandler(
            "addquestion",
            addquestion
        )
    )

    application.add_handler(
        CommandHandler(
            "questions",
            questions
        )
    )

    application.add_handler(
        CommandHandler(
            "delquestion",
            delquestion
        )
    )

    application.add_handler(
        CommandHandler(
            "enablequestion",
            enablequestion
        )
    )

    application.add_handler(
        CommandHandler(
            "disablequestion",
            disablequestion
        )
    )

    # -----------------------------------------
    # QUIZ ANSWERS
    # -----------------------------------------

    application.add_handler(
        PollAnswerHandler(
            quiz_answer
        )
    )

    # -----------------------------------------
    # MARKET
    # -----------------------------------------

    application.add_handler(
        CommandHandler(
            "market",
            market
        )
    )

    application.add_handler(
        CommandHandler(
            "buy",
            buy
        )
    )

    application.add_handler(
        CommandHandler(
            "sell",
            sell
        )
    )

    application.add_handler(
        CommandHandler(
            "portfolio",
            portfolio
        )
    )

    application.add_handler(
        CommandHandler(
            "setmarketgroup",
            setmarketgroup
        )
    )

    application.add_handler(
        CommandHandler(
            "unsetmarketgroup",
            unsetmarketgroup
        )
    )

    application.add_handler(
        CommandHandler(
            "setprice",
            setprice
        )
    )

    # -----------------------------------------
    # COIN WORDS
    # -----------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            coin_message_handler
        )
    )

    application.add_error_handler(
        error_handler
    )

    # -----------------------------------------
    # RUN
    # -----------------------------------------

    logger.info(
        "Bot is running..."
    )

    application.run_polling(
        drop_pending_updates=True
    )


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":
    main()