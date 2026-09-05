import os
import time
import json
import logging
from threading import Thread
import random

import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from flask import Flask, request, jsonify

from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# =========================================================
# SETTINGS
# =========================================================

TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

ADMIN_ID = 6235380364

COINS_PER_MESSAGE = 10
COOLDOWN = 2 * 60

GAME_URL = "https://t-pk89.onrender.com/"
COINS_PER_GAME_SCORE = 5

PORT = int(os.environ.get("PORT", 10000))


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

    try:
        db_pool = ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=DATABASE_URL,
            connect_timeout=10,
            sslmode="require",
        )

        logger.info("PostgreSQL connection pool created.")

    except Exception:
        logger.exception("Could not create PostgreSQL pool.")
        raise


def get_db():
    global db_pool

    if db_pool is None:
        create_db_pool()

    return db_pool.getconn()


def release_db(conn):
    if conn is None:
        return

    try:
        if db_pool:
            db_pool.putconn(conn)
    except Exception:
        logger.exception("Could not release database connection.")


def init_db():
    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                name TEXT NOT NULL,
                coins INTEGER NOT NULL DEFAULT 0,
                last_message DOUBLE PRECISION NOT NULL DEFAULT 0
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS game_scores (
                user_id BIGINT PRIMARY KEY,
                name TEXT NOT NULL,
                score INTEGER NOT NULL DEFAULT 0,
                games_played INTEGER NOT NULL DEFAULT 0,
                best_score INTEGER NOT NULL DEFAULT 0
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS game_results (
                game_id TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                score INTEGER NOT NULL,
                coins_awarded INTEGER NOT NULL,
                created_at DOUBLE PRECISION NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_groups (
                chat_id BIGINT PRIMARY KEY,
                title TEXT NOT NULL
            )
        """)

        conn.commit()

        logger.info("Database initialized successfully.")

    except Exception:
        if conn:
            conn.rollback()

        logger.exception("Database initialization failed.")

    finally:
        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# DATABASE HELPERS
# =========================================================

def get_user(user_id, name):
    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT coins, last_message
            FROM users
            WHERE user_id = %s
        """, (user_id,))

        row = cur.fetchone()

        if row is None:

            cur.execute("""
                INSERT INTO users
                (user_id, name, coins, last_message)
                VALUES (%s, %s, 0, 0)
                ON CONFLICT (user_id) DO NOTHING
            """, (user_id, name))

            conn.commit()

            return 0, 0

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


def update_user(user_id, name, coins, last):
    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO users
            (
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
            last
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
# /START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return

    keyboard = [[
        KeyboardButton(
            "🎮 بازی Subway Bird",
            web_app=WebAppInfo(url=GAME_URL)
        )
    ]]

    await update.message.reply_text(
        "🤖 ربات کوین فعاله!\n\n"
        "🎮 Subway Bird\n"
        "🪙 هر امتیاز بازی = ۵ کوین\n\n"
        "🗣 فولک = ۱۰ کوین\n"
        "💰 /balance = موجودی\n"
        "🏆 /top = جدول کوین‌ها\n"
        "🎮 /gametop = جدول رکورد بازی\n"
        "📊 /gamestats = آمار بازی من\n\n"
        "👑 دستورات ادمین:\n"
        "➕ /addcoins 1000\n"
        "➖ /removecoins 1000\n"
        "📊 /playerstats = آمار بازیکن با Reply\n"
        "📢 /say متن\n"
        "📢 /groupmsg متن\n"
        "⚙️ /setgroup",
        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True
        )
    )


# =========================================================
# FOLK
# =========================================================

async def message_handler(
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

    if message.text.strip() != "فولک":
        return

    user = message.from_user

    user_id = user.id
    name = user.first_name or "کاربر"

    now = time.time()

    try:
        coins, last = get_user(
            user_id,
            name
        )

        if now - last < COOLDOWN:
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
        logger.exception("FOLK ERROR")

        await message.reply_text(
            "❌ موقتاً خطایی رخ داد، دوباره امتحان کن."
        )


# =========================================================
# BALANCE
# =========================================================

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):

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
            f"💰 موجودی شما: {coins} کوین"
        )

    except Exception:
        logger.exception("BALANCE ERROR")

        await update.message.reply_text(
            "❌ خطا در گرفتن موجودی."
        )


# =========================================================
# COIN TOP
# =========================================================

async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):

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

        ranking = cur.fetchall()

        if not ranking:
            await update.message.reply_text(
                "🏆 هنوز کسی کوین نداره!"
            )
            return

        text = "🏆 جدول ۱۰ نفر برتر\n\n"

        medals = [
            "🥇",
            "🥈",
            "🥉"
        ]

        for i, (name, coins) in enumerate(
            ranking,
            1
        ):

            prefix = (
                medals[i - 1]
                if i <= 3
                else f"{i}."
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
            "❌ خطا در جدول امتیازات."
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# GAME TOP
# =========================================================

async def gametop(
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
            SELECT name, best_score
            FROM game_scores
            ORDER BY best_score DESC
            LIMIT 10
        """)

        ranking = cur.fetchall()

        if not ranking:

            await update.message.reply_text(
                "🎮 هنوز کسی بازی نکرده!"
            )

            return

        text = (
            "🎮 جدول رکورد Subway Bird\n\n"
        )

        medals = [
            "🥇",
            "🥈",
            "🥉"
        ]

        for i, (name, score) in enumerate(
            ranking,
            1
        ):

            prefix = (
                medals[i - 1]
                if i <= 3
                else f"{i}."
            )

            text += (
                f"{prefix} "
                f"{name} — "
                f"{score} امتیاز\n"
            )

        await update.message.reply_text(text)

    except Exception:

        logger.exception("GAMETOP ERROR")

        await update.message.reply_text(
            "❌ خطا در جدول بازی."
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# GAME STATS
# =========================================================

async def gamestats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if not update.message:
        return

    user = update.effective_user

    user_id = user.id
    name = user.first_name or "کاربر"

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                name,
                score,
                games_played,
                best_score
            FROM game_scores
            WHERE user_id = %s
        """, (user_id,))

        result = cur.fetchone()

        if not result:

            await update.message.reply_text(
                "🎮 هنوز هیچ بازی‌ای انجام ندادی!"
            )

            return

        db_name = result[0]
        last_score = result[1]
        games_played = result[2]
        best_score = result[3]

        cur.execute("""
            SELECT COALESCE(SUM(score), 0)
            FROM game_results
            WHERE user_id = %s
        """, (user_id,))

        total_score = cur.fetchone()[0]

        await update.message.reply_text(
            f"🎮 آمار Subway Bird\n\n"
            f"👤 بازیکن: {db_name}\n"
            f"🕹 تعداد بازی: {games_played}\n"
            f"📊 آخرین امتیاز: {last_score}\n"
            f"➕ مجموع امتیازها: {total_score}\n"
            f"🏆 رکورد: {best_score}"
        )

    except Exception:

        logger.exception("GAME STATS ERROR")

        await update.message.reply_text(
            "❌ خطا در گرفتن آمار."
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
            "❌ باید روی پیام شخص Reply کنی "
            "و بعد /playerstats رو بفرستی."
        )

        return

    target = reply.from_user

    if not target:

        await update.message.reply_text(
            "❌ نتونستم کاربر رو شناسایی کنم."
        )

        return

    user_id = target.id
    name = target.first_name or "کاربر"

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                name,
                score,
                games_played,
                best_score
            FROM game_scores
            WHERE user_id = %s
        """, (user_id,))

        result = cur.fetchone()

        if not result:

            await update.message.reply_text(
                f"🎮 {name} هنوز هیچ بازی‌ای ثبت نکرده."
            )

            return

        db_name = result[0]
        last_score = result[1]
        games_played = result[2]
        best_score = result[3]

        cur.execute("""
            SELECT COALESCE(SUM(score), 0)
            FROM game_results
            WHERE user_id = %s
        """, (user_id,))

        total_score = cur.fetchone()[0]

        await update.message.reply_text(
            f"🎮 آمار بازیکن\n\n"
            f"👤 نام: {db_name}\n"
            f"🆔 ID: {user_id}\n"
            f"🕹 تعداد بازی: {games_played}\n"
            f"📊 آخرین امتیاز: {last_score}\n"
            f"➕ مجموع امتیازها: {total_score}\n"
            f"🏆 رکورد: {best_score}"
        )

    except Exception:

        logger.exception("PLAYER STATS ERROR")

        await update.message.reply_text(
            "❌ خطا در گرفتن آمار بازیکن."
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# GAME WEB APP DATA
# =========================================================

async def web_app_data(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.effective_message

    if not message:
        return

    if not message.web_app_data:
        return

    if not message.from_user:
        return

    user = message.from_user

    user_id = user.id
    name = user.first_name or "کاربر"

    try:

        data = json.loads(
            message.web_app_data.data
        )

    except Exception:

        await message.reply_text(
            "❌ اطلاعات بازی نامعتبر است."
        )

        return

    if data.get("action") != "game_over":
        return

    try:

        score = int(
            data.get(
                "score",
                0
            )
        )

    except Exception:

        await message.reply_text(
            "❌ امتیاز نامعتبر است."
        )

        return

    if score < 0 or score > 100000:

        await message.reply_text(
            "❌ امتیاز غیرمجاز است."
        )

        return

    game_id = str(
        data.get(
            "game_id",
            ""
        )
    ).strip()

    if not game_id:

        await message.reply_text(
            "❌ شناسه بازی وجود ندارد."
        )

        return

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        # جلوگیری از ثبت دوباره یک بازی

        cur.execute("""
            SELECT 1
            FROM game_results
            WHERE game_id = %s
        """, (game_id,))

        if cur.fetchone():

            conn.rollback()

            await message.reply_text(
                "⚠️ این بازی قبلاً ثبت شده."
            )

            return

        coins_awarded = (
            score *
            COINS_PER_GAME_SCORE
        )

        # ثبت نتیجه بازی

        cur.execute("""
            INSERT INTO game_results
            (
                game_id,
                user_id,
                score,
                coins_awarded,
                created_at
            )
            VALUES
            (%s, %s, %s, %s, %s)
        """, (
            game_id,
            user_id,
            score,
            coins_awarded,
            time.time()
        ))

        # ثبت آمار بازیکن

        cur.execute("""
            INSERT INTO game_scores
            (
                user_id,
                name,
                score,
                games_played,
                best_score
            )
            VALUES
            (%s, %s, %s, 1, %s)

            ON CONFLICT (user_id)
            DO UPDATE SET

                name = EXCLUDED.name,

                score = EXCLUDED.score,

                games_played =
                    game_scores.games_played + 1,

                best_score =
                    GREATEST(
                        game_scores.best_score,
                        EXCLUDED.score
                    )
        """, (
            user_id,
            name,
            score,
            score
        ))

        # اضافه کردن کوین

        cur.execute("""
            INSERT INTO users
            (
                user_id,
                name,
                coins,
                last_message
            )
            VALUES
            (%s, %s, %s, 0)

            ON CONFLICT (user_id)
            DO UPDATE SET

                name = EXCLUDED.name,

                coins =
                    users.coins +
                    EXCLUDED.coins
        """, (
            user_id,
            name,
            coins_awarded
        ))

        conn.commit()

    except psycopg2.IntegrityError:

        if conn:
            conn.rollback()

        logger.exception(
            "GAME DATABASE INTEGRITY ERROR"
        )

        await message.reply_text(
            "⚠️ این بازی قبلاً ثبت شده یا "
            "اطلاعات تکراری است."
        )

        return

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "GAME ERROR"
        )

        await message.reply_text(
            "❌ خطا در ثبت بازی."
        )

        return

    finally:

        if cur:
            cur.close()

        release_db(conn)

    await message.reply_text(
        f"🎮 بازی تموم شد!\n\n"
        f"🏆 امتیاز: {score}\n"
        f"🪙 جایزه: +{coins_awarded} کوین"
    )


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
            "❌ مقدار کوین را وارد کن."
        )

        return

    try:

        amount = int(
            context.args[0]
        )

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

    try:

        coins, last = get_user(
            target.id,
            name
        )

        update_user(
            target.id,
            name,
            coins + amount,
            last
        )

        await update.message.reply_text(
            f"✅ {amount} 🪙 به "
            f"{name} اضافه شد."
        )

    except Exception:

        logger.exception("ADD COINS ERROR")

        await update.message.reply_text(
            "❌ خطا در اضافه کردن کوین."
        )


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
            "❌ مقدار کوین را وارد کن."
        )

        return

    try:

        amount = int(
            context.args[0]
        )

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

    try:

        coins, last = get_user(
            target.id,
            name
        )

        removed = min(
            amount,
            coins
        )

        update_user(
            target.id,
            name,
            coins - removed,
            last
        )

        await update.message.reply_text(
            f"✅ {removed} 🪙 از "
            f"{name} کم شد."
        )

    except Exception:

        logger.exception(
            "REMOVE COINS ERROR"
        )

        await update.message.reply_text(
            "❌ خطا در کم کردن کوین."
        )


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

    if not context.args:
        return

    await update.message.reply_text(
        " ".join(context.args)
    )


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

    if not update.message:
        return

    if update.message.chat.type not in (
        "group",
        "supergroup"
    ):

        await update.message.reply_text(
            "❌ این دستور رو داخل گروه بزن."
        )

        return

    chat = update.message.chat

    title = chat.title or "گروه"

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO bot_groups
            (
                chat_id,
                title
            )
            VALUES
            (%s, %s)

            ON CONFLICT (chat_id)
            DO UPDATE SET
                title = EXCLUDED.title
        """, (
            chat.id,
            title
        ))

        conn.commit()

        await update.message.reply_text(
            f"✅ گروه «{title}» ثبت شد."
        )

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "SET GROUP ERROR"
        )

        await update.message.reply_text(
            "❌ خطا در ثبت گروه."
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)


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

    if not context.args:

        await update.message.reply_text(
            "❌ مثال:\n"
            "/groupmsg سلام بچه‌ها 👋"
        )

        return

    text = " ".join(
        context.args
    )

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

    except Exception:

        logger.exception(
            "GET GROUPS ERROR"
        )

        await update.message.reply_text(
            "❌ خطا در گرفتن گروه‌ها."
        )

        return

    finally:

        if cur:
            cur.close()

        release_db(conn)

    if not groups:

        await update.message.reply_text(
            "❌ هنوز گروهی ثبت نشده."
        )

        return

    sent = 0
    failed = 0

    for (chat_id,) in groups:

        try:

            await context.bot.send_message(
                chat_id=chat_id,
                text=text
            )

            sent += 1

        except Exception:

            logger.exception(
                "GROUP SEND ERROR"
            )

            failed += 1

    await update.message.reply_text(
        f"📢 ارسال شد!\n\n"
        f"✅ موفق: {sent}\n"
        f"❌ ناموفق: {failed}"
    )


# =========================================================
# FLASK
# =========================================================

web = Flask(__name__)


@web.get("/")
def home():

    return jsonify({
        "ok": True,
        "service": "telegram-bot",
        "status": "running"
    }), 200


@web.get("/health")
def health():

    return jsonify({
        "ok": True,
        "status": "healthy"
    }), 200


@web.after_request
def cors(response):

    response.headers[
        "Access-Control-Allow-Origin"
    ] = "*"

    response.headers[
        "Access-Control-Allow-Headers"
    ] = "Content-Type"
    
    response.headers[
        "Access-Control-Allow-Methods"
    ] = "GET, POST, OPTIONS"

    return response


@web.post("/game-score")
def game_score_api():

    data = request.get_json(
        silent=True
    ) or {}

    try:

        user_id = int(
            data.get(
                "user_id",
                0
            )
        )

        score = int(
            data.get(
                "score",
                0
            )
        )

    except Exception:

        return jsonify({
            "ok": False,
            "error": "invalid data"
        }), 400

    if user_id <= 0:

        return jsonify({
            "ok": False,
            "error": "invalid user"
        }), 400

    if score < 0 or score > 100000:

        return jsonify({
            "ok": False,
            "error": "invalid score"
        }), 400

    name = str(
        data.get(
            "name",
            "کاربر"
        )
    )[:100]

    game_id = str(
        data.get(
            "game_id",
            ""
        )
    ).strip()

    if not game_id:

        return jsonify({
            "ok": False,
            "error": "missing game_id"
        }), 400

    coins_awarded = (
        score *
        COINS_PER_GAME_SCORE
    )

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        # جلوگیری از ثبت دوباره

        cur.execute("""
            SELECT 1
            FROM game_results
            WHERE game_id = %s
        """, (game_id,))

        if cur.fetchone():

            return jsonify({
                "ok": False,
                "error": "game already registered"
            }), 409

        # ثبت بازی

        cur.execute("""
            INSERT INTO game_results
            (
                game_id,
                user_id,
                score,
                coins_awarded,
                created_at
            )
            VALUES
            (%s, %s, %s, %s, %s)
        """, (
            game_id,
            user_id,
            score,
            coins_awarded,
            time.time()
        ))

        # ثبت آمار

        cur.execute("""
            INSERT INTO game_scores
            (
                user_id,
                name,
                score,
                games_played,
                best_score
            )
            VALUES
            (%s, %s, %s, 1, %s)

            ON CONFLICT (user_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                score = EXCLUDED.score,
                games_played =
                    game_scores.games_played + 1,
                best_score =
                    GREATEST(
                        game_scores.best_score,
                        EXCLUDED.score
                    )
        """, (
            user_id,
            name,
            score,
            score
        ))

        # اضافه کردن کوین

        cur.execute("""
            INSERT INTO users
            (
                user_id,
                name,
                coins,
                last_message
            )
            VALUES
            (%s, %s, %s, 0)

            ON CONFLICT (user_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                coins =
                    users.coins +
                    EXCLUDED.coins
        """, (
            user_id,
            name,
            coins_awarded
        ))

        conn.commit()

        return jsonify({
            "ok": True,
            "score": score,
            "coins": coins_awarded
        }), 200

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "GAME SCORE API ERROR"
        )

        return jsonify({
            "ok": False,
            "error": "database error"
        }), 500

    finally:

        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# FLASK THREAD
# =========================================================

def run_flask():

    logger.info(
        "Starting Flask on port %s",
        PORT
    )

    web.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True
    )


# =========================================================
# TELEGRAM ERROR HANDLER
# =========================================================

async def telegram_error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    logger.error(
        "Telegram update error:",
        exc_info=context.error
    )


# =========================================================
# MAIN
# =========================================================

def main():

    logger.info(
        "================================="
    )

    logger.info(
        "Starting Telegram Coin Bot..."
    )

    logger.info(
        "================================="
    )

    # Database

    create_db_pool()
    init_db()

    # Flask

    flask_thread = Thread(
        target=run_flask,
        daemon=True
    )

    flask_thread.start()

    # Telegram

    application = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    # Commands

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

    application.add_handler(
        CommandHandler(
            "gametop",
            gametop
        )
    )

    application.add_handler(
        CommandHandler(
            "gamestats",
            gamestats
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
            "say",
            say
        )
    )

    application.add_handler(
        CommandHandler(
            "setgroup",
            setgroup
        )
    )

    application.add_handler(
        CommandHandler(
            "groupmsg",
            groupmsg
        )
    )

    # فولک

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            message_handler
        )
    )

    # Telegram WebApp

    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.WEB_APP_DATA,
            web_app_data
        )
    )

    # Global Telegram error handler

    application.add_error_handler(
        telegram_error_handler
    )

    logger.info(
        "Bot is starting polling..."
    )

    try:

        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )

    except Exception:

        logger.exception(
            "BOT STOPPED WITH ERROR"
        )

        raise


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":

    main()
# =========================================================
# MARKET API
# =========================================================

MARKET_ID = 1


def market_get():
    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                name,
                price,
                min_change,
                max_change,
                auto_change
            FROM market
            WHERE id = %s
        """, (MARKET_ID,))

        row = cur.fetchone()

        if row is None:
            cur.execute("""
                INSERT INTO market (
                    id,
                    name,
                    price,
                    min_change,
                    max_change,
                    auto_change,
                    updated_at
                )
                VALUES (
                    %s,
                    'BetaCoin',
                    100,
                    -0.05,
                    0.05,
                    TRUE,
                    %s
                )
            """, (MARKET_ID, time.time()))

            conn.commit()

            return {
                "name": "BetaCoin",
                "price": 100,
                "min_change": -0.05,
                "max_change": 0.05,
                "auto_change": True
            }

        return {
            "name": row[0],
            "price": float(row[1]),
            "min_change": float(row[2]),
            "max_change": float(row[3]),
            "auto_change": bool(row[4])
        }

    finally:
        if cur:
            cur.close()

        release_db(conn)


def market_change_price():
    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                price,
                min_change,
                max_change,
                auto_change
            FROM market
            WHERE id = %s
            FOR UPDATE
        """, (MARKET_ID,))

        row = cur.fetchone()

        if row is None:
            return None

        price = float(row[0])
        min_change = float(row[1])
        max_change = float(row[2])
        auto_change = bool(row[3])

        if not auto_change:
            return price

        change = random.uniform(
            min_change,
            max_change
        )

        new_price = price * (1 + change)

        # جلوگیری از صفر یا منفی شدن قیمت
        new_price = max(0.01, new_price)

        cur.execute("""
            UPDATE market
            SET price = %s,
                updated_at = %s
            WHERE id = %s
        """, (
            new_price,
            time.time(),
            MARKET_ID
        ))

        cur.execute("""
            INSERT INTO market_history (
                price,
                created_at
            )
            VALUES (%s, %s)
        """, (
            new_price,
            time.time()
        ))

        conn.commit()

        return new_price

    except Exception:
        if conn:
            conn.rollback()

        raise

    finally:
        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# MARKET INFO
# =========================================================

@web.get("/market")
def market_api():

    try:
        market = market_get()

        return jsonify({
            "ok": True,
            "market": market
        })

    except Exception:
        logger.exception("MARKET API ERROR")

        return jsonify({
            "ok": False,
            "error": "market error"
        }), 500


# =========================================================
# WALLET
# =========================================================

@web.get("/wallet/<int:user_id>")
def wallet_api(user_id):

    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT coins
            FROM users
            WHERE user_id = %s
        """, (user_id,))

        row = cur.fetchone()

        if row is None:
            return jsonify({
                "ok": True,
                "coins": 0
            })

        return jsonify({
            "ok": True,
            "coins": int(row[0])
        })

    except Exception:
        if conn:
            conn.rollback()

        logger.exception("WALLET API ERROR")

        return jsonify({
            "ok": False,
            "error": "database error"
        }), 500

    finally:
        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# HOLDING
# =========================================================

@web.get("/portfolio/<int:user_id>")
def portfolio_api(user_id):

    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT amount
            FROM market_holdings
            WHERE user_id = %s
        """, (user_id,))

        row = cur.fetchone()

        amount = 0

        if row:
            amount = float(row[0])

        market = market_get()

        value = amount * market["price"]

        return jsonify({
            "ok": True,
            "amount": amount,
            "value": value
        })

    except Exception:
        if conn:
            conn.rollback()

        logger.exception("PORTFOLIO API ERROR")

        return jsonify({
            "ok": False,
            "error": "portfolio error"
        }), 500

    finally:
        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# BUY
# =========================================================

@web.post("/buy")
def buy_api():

    conn = None
    cur = None

    try:
        data = request.get_json(silent=True) or {}

        user_id = int(data.get("user_id", 0))
        amount = float(data.get("amount", 0))

        if user_id <= 0:
            return jsonify({
                "ok": False,
                "error": "invalid user"
            }), 400

        if amount <= 0:
            return jsonify({
                "ok": False,
                "error": "invalid amount"
            }), 400

        conn = get_db()
        cur = conn.cursor()

        # Lock user row
        cur.execute("""
            SELECT coins
            FROM users
            WHERE user_id = %s
            FOR UPDATE
        """, (user_id,))

        user = cur.fetchone()

        if user is None:
            return jsonify({
                "ok": False,
                "error": "user not found"
            }), 404

        coins = int(user[0])

        market = market_get()

        price = market["price"]

        cost = amount * price

        if cost > coins:
            return jsonify({
                "ok": False,
                "error": "not enough coins"
            }), 400

        # Deduct coins
        cur.execute("""
            UPDATE users
            SET coins = coins - %s
            WHERE user_id = %s
        """, (
            int(round(cost)),
            user_id
        ))

        # Add holding
        cur.execute("""
            INSERT INTO market_holdings (
                user_id,
                amount
            )
            VALUES (%s, %s)
            ON CONFLICT (user_id)
            DO UPDATE SET
                amount = market_holdings.amount + EXCLUDED.amount
        """, (
            user_id,
            amount
        ))

        conn.commit()

        return jsonify({
            "ok": True,
            "amount": amount,
            "price": price,
            "cost": int(round(cost)),
            "coins": coins - int(round(cost))
        })

    except Exception:
        if conn:
            conn.rollback()

        logger.exception("BUY API ERROR")

        return jsonify({
            "ok": False,
            "error": "buy error"
        }), 500

    finally:
        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# SELL
# =========================================================

@web.post("/sell")
def sell_api():

    conn = None
    cur = None

    try:
        data = request.get_json(silent=True) or {}

        user_id = int(data.get("user_id", 0))
        amount = float(data.get("amount", 0))

        if user_id <= 0:
            return jsonify({
                "ok": False,
                "error": "invalid user"
            }), 400

        if amount <= 0:
            return jsonify({
                "ok": False,
                "error": "invalid amount"
            }), 400

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT amount
            FROM market_holdings
            WHERE user_id = %s
            FOR UPDATE
        """, (user_id,))

        holding = cur.fetchone()

        owned = float(holding[0]) if holding else 0

        if amount > owned:
            return jsonify({
                "ok": False,
                "error": "not enough stock"
            }), 400

        market = market_get()

        price = market["price"]

        revenue = amount * price

        # Remove holding
        cur.execute("""
            UPDATE market_holdings
            SET amount = amount - %s
            WHERE user_id = %s
        """, (
            amount,
            user_id
        ))

        # Add coins
        cur.execute("""
            UPDATE users
            SET coins = coins + %s
            WHERE user_id = %s
        """, (
            int(round(revenue)),
            user_id
        ))

        conn.commit()

        return jsonify({
            "ok": True,
            "amount": amount,
            "price": price,
            "revenue": int(round(revenue)),
            "remaining": owned - amount
        })

    except Exception:
        if conn:
            conn.rollback()

        logger.exception("SELL API ERROR")

        return jsonify({
            "ok": False,
            "error": "sell error"
        }), 500

    finally:
        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# ADMIN - CHANGE PRICE
# =========================================================

@web.post("/admin/price")
def admin_price_api():

    conn = None
    cur = None

    try:
        data = request.get_json(silent=True) or {}

        user_id = int(data.get("user_id", 0))
        price = float(data.get("price", 0))

        if user_id != ADMIN_ID:
            return jsonify({
                "ok": False,
                "error": "access denied"
            }), 403

        if price <= 0:
            return jsonify({
                "ok": False,
                "error": "invalid price"
            }), 400

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            UPDATE market
            SET price = %s,
                updated_at = %s
            WHERE id = %s
        """, (
            price,
            time.time(),
            MARKET_ID
        ))

        cur.execute("""
            INSERT INTO market_history (
                price,
                created_at
            )
            VALUES (%s, %s)
        """, (
            price,
            time.time()
        ))

        conn.commit()

        return jsonify({
            "ok": True,
            "price": price
        })

    except Exception:
        if conn:
            conn.rollback()

        logger.exception("ADMIN PRICE ERROR")

        return jsonify({
            "ok": False,
            "error": "admin error"
        }), 500

    finally:
        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# ADMIN - RANDOM SETTINGS
# =========================================================

@web.post("/admin/settings")
def admin_settings_api():

    conn = None
    cur = None

    try:
        data = request.get_json(silent=True) or {}

        user_id = int(data.get("user_id", 0))

        if user_id != ADMIN_ID:
            return jsonify({
                "ok": False,
                "error": "access denied"
            }), 403

        min_change = float(
            data.get("min_change", -5)
        ) / 100

        max_change = float(
            data.get("max_change", 5)
        ) / 100

        auto_change = bool(
            data.get("auto_change", True)
        )

        if min_change < -1:
            min_change = -1

        if max_change > 1:
            max_change = 1

        if min_change > max_change:
            return jsonify({
                "ok": False,
                "error": "invalid range"
            }), 400

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            UPDATE market
            SET min_change = %s,
                max_change = %s,
                auto_change = %s,
                updated_at = %s
            WHERE id = %s
        """, (
            min_change,
            max_change,
            auto_change,
            time.time(),
            MARKET_ID
        ))

        conn.commit()

        return jsonify({
            "ok": True,
            "min_change": min_change,
            "max_change": max_change,
            "auto_change": auto_change
        })

    except Exception:
        if conn:
            conn.rollback()

        logger.exception("ADMIN SETTINGS ERROR")

        return jsonify({
            "ok": False,
            "error": "admin error"
        }), 500

    finally:
        if cur:
            cur.close()

        release_db(conn)