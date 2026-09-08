import os
import time
import json
import logging
import random
import uuid
from threading import Thread

import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from flask import Flask, request, jsonify

from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
    Poll,
)

from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PollAnswerHandler,
    ConversationHandler,
    filters,
)


# =========================================================
# SETTINGS
# =========================================================

TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

ADMIN_ID = 6235380364

# -------------------------
# COINS
# -------------------------

COINS_PER_MESSAGE = 10
COOLDOWN = 2 * 60

# -------------------------
# GAME
# -------------------------

GAME_URL = "https://t-pk89.onrender.com/"
COINS_PER_GAME_SCORE = 5

# -------------------------
# QUIZ
# -------------------------

QUIZ_REWARD = 10

# -------------------------
# MARKET
# -------------------------

ANGRYCOIN_NAME = "AngryCoin"
DEFAULT_ANGRYCOIN_PRICE = 100

# گروهی که بورس در آن فعال است
MARKET_GROUP_ID = None

# -------------------------
# FLASK
# -------------------------

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

    db_pool = ThreadedConnectionPool(
        minconn=1,
        maxconn=10,
        dsn=DATABASE_URL,
        connect_timeout=10,
        sslmode="require",
    )

    logger.info("PostgreSQL connection pool created.")


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


# =========================================================
# DATABASE INIT
# =========================================================

def init_db():

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        # =================================================
        # USERS
        # =================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                name TEXT NOT NULL,
                coins INTEGER NOT NULL DEFAULT 0,
                last_message DOUBLE PRECISION NOT NULL DEFAULT 0
            )
        """)

        # =================================================
        # GAME
        # =================================================

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

        # =================================================
        # GROUPS
        # =================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_groups (
                chat_id BIGINT PRIMARY KEY,
                title TEXT NOT NULL
            )
        """)

        # =================================================
        # QUIZ QUESTIONS
        # =================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS quiz_questions (
                id SERIAL PRIMARY KEY,
                question TEXT NOT NULL,
                option1 TEXT NOT NULL,
                option2 TEXT NOT NULL,
                option3 TEXT NOT NULL,
                option4 TEXT NOT NULL,
                correct_option INTEGER NOT NULL,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at DOUBLE PRECISION NOT NULL
            )
        """)

        # =================================================
        # QUIZ POLLS
        # =================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS quiz_polls (
                poll_id TEXT PRIMARY KEY,
                question_id INTEGER NOT NULL,
                chat_id BIGINT,
                message_id BIGINT,
                created_at DOUBLE PRECISION NOT NULL
            )
        """)

        # =================================================
        # QUIZ ANSWERS
        # =================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS quiz_answers (
                poll_id TEXT NOT NULL,
                user_id BIGINT NOT NULL,
                selected_option INTEGER NOT NULL,
                correct BOOLEAN NOT NULL,
                reward INTEGER NOT NULL DEFAULT 0,
                created_at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (poll_id, user_id)
            )
        """)

        # =================================================
        # QUIZ SCORES
        # =================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS quiz_scores (
                user_id BIGINT PRIMARY KEY,
                name TEXT NOT NULL,
                correct_answers INTEGER NOT NULL DEFAULT 0,
                total_answers INTEGER NOT NULL DEFAULT 0,
                score INTEGER NOT NULL DEFAULT 0
            )
        """)

        # =================================================
        # ANGRYCOIN MARKET
        # =================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS angrycoin_market (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                price DOUBLE PRECISION NOT NULL,
                group_id BIGINT,
                updated_at DOUBLE PRECISION NOT NULL
            )
        """)

        # =================================================
        # ANGRYCOIN HOLDINGS
        # =================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS angrycoin_holdings (
                user_id BIGINT PRIMARY KEY,
                amount DOUBLE PRECISION NOT NULL DEFAULT 0
            )
        """)

        # =================================================
        # ANGRYCOIN HISTORY
        # =================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS angrycoin_history (
                id SERIAL PRIMARY KEY,
                price DOUBLE PRECISION NOT NULL,
                created_at DOUBLE PRECISION NOT NULL
            )
        """)

        # =================================================
        # DEFAULT MARKET
        # =================================================

        cur.execute("""
            SELECT id
            FROM angrycoin_market
            WHERE id = 1
        """)

        if cur.fetchone() is None:

            cur.execute("""
                INSERT INTO angrycoin_market
                (
                    id,
                    name,
                    price,
                    group_id,
                    updated_at
                )
                VALUES
                (1, %s, %s, NULL, %s)
            """, (
                ANGRYCOIN_NAME,
                DEFAULT_ANGRYCOIN_PRICE,
                time.time()
            ))

            cur.execute("""
                INSERT INTO angrycoin_history
                (
                    price,
                    created_at
                )
                VALUES
                (%s, %s)
            """, (
                DEFAULT_ANGRYCOIN_PRICE,
                time.time()
            ))

        conn.commit()

        logger.info("Database initialized successfully.")

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
            SELECT coins, last_message
            FROM users
            WHERE user_id = %s
        """, (user_id,))

        row = cur.fetchone()

        if row is None:

            cur.execute("""
                INSERT INTO users
                (
                    user_id,
                    name,
                    coins,
                    last_message
                )
                VALUES
                (%s, %s, 0, 0)
                ON CONFLICT (user_id) DO NOTHING
            """, (
                user_id,
                name
            ))

            conn.commit()

            return 0, 0

        return row[0], row[1]

    finally:

        if cur:
            cur.close()

        release_db(conn)


def update_user(
    user_id,
    name,
    coins,
    last
):

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
            VALUES
            (%s, %s, %s, %s)

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

        raise

    finally:

        if cur:
            cur.close()

        release_db(conn)


def add_coins(user_id, name, amount):

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
            VALUES
            (%s, %s, %s, 0)

            ON CONFLICT (user_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                coins = users.coins + EXCLUDED.coins
        """, (
            user_id,
            name,
            amount
        ))

        conn.commit()

    except Exception:

        if conn:
            conn.rollback()

        raise

    finally:

        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    keyboard = [[
        KeyboardButton(
            "🎮 بازی Subway Bird",
            web_app=WebAppInfo(
                url=GAME_URL
            )
        )
    ]]

    await update.message.reply_text(
        "🤖 ربات فعاله!\n\n"

        "🪙 فولک = ۱۰ کوین\n"
        "🐰 هاپهاپ = ۱۰ کوین\n\n"

        "💰 /balance\n"
        "🏆 /top\n\n"

        "🎮 /gametop\n"
        "📊 /gamestats\n\n"

        "🧠 /quiz\n"
        "🧠 /quizscore\n"
        "🏆 /quiztop\n\n"

        "📈 /market\n"
        "💰 /portfolio\n"
        "📈 /buy مقدار\n"
        "📉 /sell مقدار\n\n"

        "👑 دستورات ادمین:\n"
        "/addcoins مقدار\n"
        "/removecoins مقدار\n"
        "/playerstats\n"
        "/say متن\n"
        "/setgroup\n"
        "/groupmsg متن\n"
        "/addquestion\n"
        "/setprice قیمت\n"
        "/setmarketgroup",

        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True
        )
    )


# =========================================================
# FOLK + HAP HAP
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

    text = message.text.strip()

    if text not in (
        "فولک",
        "هاپهاپ"
    ):
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

        # کول‌داون مشترک
        if now - last < COOLDOWN:
            return

        coins += COINS_PER_MESSAGE

        update_user(
            user_id,
            name,
            coins,
            now
        )

        if text == "فولک":
            emoji = "🗣️"
        else:
            emoji = "🐰"

        await message.reply_text(
            f"{emoji} {name} +{COINS_PER_MESSAGE} 🪙 گرفت!"
        )

    except Exception:

        logger.exception(
            "MESSAGE COIN ERROR"
        )

        await message.reply_text(
            "❌ موقتاً خطایی رخ داد."
        )


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

        logger.exception(
            "BALANCE ERROR"
        )

        await update.message.reply_text(
            "❌ خطا در گرفتن موجودی."
        )


# =========================================================
# TOP
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

        for i, row in enumerate(
            ranking,
            1
        ):

            name = row[0]
            coins = row[1]

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

        await update.message.reply_text(
            text
        )

    except Exception:

        logger.exception(
            "TOP ERROR"
        )

        await update.message.reply_text(
            "❌ خطا در جدول."
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

        for i, row in enumerate(
            ranking,
            1
        ):

            name = row[0]
            score = row[1]

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

        await update.message.reply_text(
            text
        )

    except Exception:

        logger.exception(
            "GAME TOP ERROR"
        )

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
        """, (
            user.id,
        ))

        result = cur.fetchone()

        if not result:

            await update.message.reply_text(
                "🎮 هنوز بازی نکردی!"
            )

            return

        name = result[0]
        score = result[1]
        games_played = result[2]
        best_score = result[3]

        cur.execute("""
            SELECT COALESCE(
                SUM(score),
                0
            )
            FROM game_results
            WHERE user_id = %s
        """, (
            user.id,
        ))

        total_score = cur.fetchone()[0]

        await update.message.reply_text(
            f"🎮 آمار Subway Bird\n\n"
            f"👤 {name}\n"
            f"🕹 بازی‌ها: {games_played}\n"
            f"📊 آخرین امتیاز: {score}\n"
            f"➕ مجموع: {total_score}\n"
            f"🏆 رکورد: {best_score}"
        )

    except Exception:

        logger.exception(
            "GAME STATS ERROR"
        )

        await update.message.reply_text(
            "❌ خطا در آمار."
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
                score,
                games_played,
                best_score
            FROM game_scores
            WHERE user_id = %s
        """, (
            target.id,
        ))

        result = cur.fetchone()

        if not result:

            await update.message.reply_text(
                "❌ بازیکن اطلاعاتی نداره."
            )

            return

        cur.execute("""
            SELECT COALESCE(
                SUM(score),
                0
            )
            FROM game_results
            WHERE user_id = %s
        """, (
            target.id,
        ))

        total_score = cur.fetchone()[0]

        await update.message.reply_text(
            f"🎮 آمار بازیکن\n\n"
            f"👤 {result[0]}\n"
            f"🆔 {target.id}\n"
            f"🕹 بازی‌ها: {result[2]}\n"
            f"📊 آخرین: {result[1]}\n"
            f"➕ مجموع: {total_score}\n"
            f"🏆 رکورد: {result[3]}"
        )

    except Exception:

        logger.exception(
            "PLAYER STATS ERROR"
        )

        await update.message.reply_text(
            "❌ خطا."
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
            "❌ مثال:\n/addcoins 100"
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

        add_coins(
            target.id,
            name,
            amount
        )

        await update.message.reply_text(
            f"✅ {amount} 🪙 به {name} اضافه شد."
        )

    except Exception:

        logger.exception(
            "ADD COINS ERROR"
        )

        await update.message.reply_text(
            "❌ خطا."
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
        return

    try:

        amount = int(
            context.args[0]
        )

    except ValueError:
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
            SELECT coins
            FROM users
            WHERE user_id = %s
            FOR UPDATE
        """, (
            target.id,
        ))

        row = cur.fetchone()

        coins = int(row[0]) if row else 0

        removed = min(
            amount,
            coins
        )

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
                coins = EXCLUDED.coins
        """, (
            target.id,
            name,
            coins - removed
        ))

        conn.commit()

        await update.message.reply_text(
            f"✅ {removed} 🪙 از {name} کم شد."
        )

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "REMOVE COINS ERROR"
        )

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
            chat.title or "گروه"
        ))

        conn.commit()

        await update.message.reply_text(
            f"✅ گروه «{chat.title}» ثبت شد."
        )

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "SET GROUP ERROR"
        )

        await update.message.reply_text(
            "❌ خطا."
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

        return

    finally:

        if cur:
            cur.close()

        release_db(conn)

    sent = 0

    for row in groups:

        try:

            await context.bot.send_message(
                chat_id=row[0],
                text=text
            )

            sent += 1

        except Exception:
            logger.exception(
                "GROUP SEND ERROR"
            )

    await update.message.reply_text(
        f"📢 ارسال شد!\n\n"
        f"✅ موفق: {sent}"
    )


# =========================================================
# QUIZ - SEND QUESTION
# =========================================================

async def quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_message:
        return

    message = update.effective_message

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
            WHERE active = TRUE
            ORDER BY RANDOM()
            LIMIT 1
        """)

        question = cur.fetchone()

        if not question:

            await message.reply_text(
                "🧠 هنوز سوالی اضافه نشده!"
            )

            return

        question_id = question[0]

        options = [
            question[2],
            question[3],
            question[4],
            question[5]
        ]

        sent = await context.bot.send_poll(
            chat_id=message.chat_id,
            question=question[1],
            options=options,
            type=Poll.QUIZ,
            correct_option_id=question[6],
            is_anonymous=False
        )

        cur.execute("""
            INSERT INTO quiz_polls
            (
                poll_id,
                question_id,
                chat_id,
                message_id,
                created_at
            )
            VALUES
            (%s, %s, %s, %s, %s)
        """, (
            sent.poll.id,
            question_id,
            message.chat_id,
            sent.message_id,
            time.time()
        ))

        conn.commit()

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "QUIZ ERROR"
        )

        await message.reply_text(
            "❌ خطا در ارسال سوال."
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# QUIZ ANSWER
# =========================================================

async def quiz_answer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    answer = update.poll_answer

    if not answer:
        return

    if not answer.user:
        return

    if not answer.option_ids:
        return

    poll_id = answer.poll_id
    user_id = answer.user.id
    selected = answer.option_ids[0]

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        # پیدا کردن سوال
        cur.execute("""
            SELECT question_id
            FROM quiz_polls
            WHERE poll_id = %s
        """, (
            poll_id,
        ))

        poll = cur.fetchone()

        if not poll:
            return

        question_id = poll[0]

        cur.execute("""
            SELECT
                correct_option
            FROM quiz_questions
            WHERE id = %s
        """, (
            question_id,
        ))

        question = cur.fetchone()

        if not question:
            return

        correct_option = question[0]

        is_correct = (
            selected == correct_option
        )

        reward = (
            QUIZ_REWARD
            if is_correct
            else 0
        )

        # جلوگیری از دوباره جایزه گرفتن
        cur.execute("""
            INSERT INTO quiz_answers
            (
                poll_id,
                user_id,
                selected_option,
                correct,
                reward,
                created_at
            )
            VALUES
            (%s, %s, %s, %s, %s, %s)

            ON CONFLICT (poll_id, user_id)
            DO NOTHING
        """, (
            poll_id,
            user_id,
            selected,
            is_correct,
            reward,
            time.time()
        ))

        if cur.rowcount == 0:

            conn.rollback()
            return

        name = (
            answer.user.first_name
            or "کاربر"
        )

        # ثبت امتیاز Quiz
        cur.execute("""
            INSERT INTO quiz_scores
            (
                user_id,
                name,
                correct_answers,
                total_answers,
                score
            )
            VALUES
            (%s, %s, %s, 1, %s)

            ON CONFLICT (user_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                correct_answers =
                    quiz_scores.correct_answers
                    + EXCLUDED.correct_answers,
                total_answers =
                    quiz_scores.total_answers + 1,
                score =
                    quiz_scores.score
                    + EXCLUDED.score
        """, (
            user_id,
            name,
            1 if is_correct else 0,
            reward
        ))

        # جایزه کوین
        if reward > 0:

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
                        users.coins
                        + EXCLUDED.coins
            """, (
                user_id,
                name,
                reward
            ))

        conn.commit()

        if reward > 0:

            try:

                await context.bot.send_message(
                    chat_id=poll[0] and (
                        await get_poll_chat_id(
                            poll_id
                        )
                    ),
                    text=(
                        f"🧠 آفرین {name}!\n"
                        f"✅ جواب درست بود!\n"
                        f"🪙 +{reward} کوین"
                    )
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


async def get_poll_chat_id(poll_id):

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT chat_id
            FROM quiz_polls
            WHERE poll_id = %s
        """, (
            poll_id,
        ))

        row = cur.fetchone()

        return row[0] if row else None

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

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                correct_answers,
                total_answers,
                score
            FROM quiz_scores
            WHERE user_id = %s
        """, (
            update.effective_user.id,
        ))

        row = cur.fetchone()

        if not row:

            await update.message.reply_text(
                "🧠 هنوز هیچ سوالی جواب ندادی!"
            )

            return

        await update.message.reply_text(
            f"🧠 آمار Quiz\n\n"
            f"✅ درست: {row[0]}\n"
            f"📊 پاسخ‌ها: {row[1]}\n"
            f"🏆 امتیاز: {row[2]}\n"
            f"🪙 جایزه: {row[2]} کوین"
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
                name,
                correct_answers,
                score
            FROM quiz_scores
            ORDER BY score DESC
            LIMIT 10
        """)

        rows = cur.fetchall()

        if not rows:

            await update.message.reply_text(
                "🏆 هنوز کسی Quiz بازی نکرده!"
            )

            return

        text = "🧠🏆 برترین‌های Quiz\n\n"

        for i, row in enumerate(
            rows,
            1
        ):

            text += (
                f"{i}. {row[0]} — "
                f"{row[2]} امتیاز "
                f"({row[1]} درست)\n"
            )

        await update.message.reply_text(
            text
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)


# =========================================================
# ADD QUESTION
# =========================================================

ADD_Q_TEXT = 1
ADD_Q_1 = 2
ADD_Q_2 = 3
ADD_Q_3 = 4
ADD_Q_4 = 5
ADD_Q_CORRECT = 6


async def addquestion_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return ConversationHandler.END

    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END

    if not update.message:
        return ConversationHandler.END

    await update.message.reply_text(
        "🧠 متن سوال رو بفرست:"
    )

    return ADD_Q_TEXT


async def addquestion_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.user_data["quiz_question"] = (
        update.message.text
    )

    await update.message.reply_text(
        "1️⃣ گزینه اول:"
    )

    return ADD_Q_1


async def addquestion_1(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.user_data["quiz_1"] = (
        update.message.text
    )

    await update.message.reply_text(
        "2️⃣ گزینه دوم:"
    )

    return ADD_Q_2


async def addquestion_2(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.user_data["quiz_2"] = (
        update.message.text
    )

    await update.message.reply_text(
        "3️⃣ گزینه سوم:"
    )

    return ADD_Q_3


async def addquestion_3(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.user_data["quiz_3"] = (
        update.message.text
    )

    await update.message.reply_text(
        "4️⃣ گزینه چهارم:"
    )

    return ADD_Q_4


async def addquestion_4(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.user_data["quiz_4"] = (
        update.message.text
    )

    await update.message.reply_text(
        "✅ شماره جواب صحیح رو بفرست:\n"
        "1 / 2 / 3 / 4"
    )

    return ADD_Q_CORRECT


async def addquestion_correct(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return ConversationHandler.END

    try:

        correct = int(
            update.message.text.strip()
        )

    except ValueError:

        await update.message.reply_text(
            "❌ فقط عدد 1 تا 4."
        )

        return ADD_Q_CORRECT

    if correct not in (
        1,
        2,
        3,
        4
    ):

        await update.message.reply_text(
            "❌ فقط 1 تا 4."
        )

        return ADD_Q_CORRECT

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO quiz_questions
            (
                question,
                option1,
                option2,
                option3,
                option4,
                correct_option,
                active,
                created_at
            )
            VALUES
            (%s, %s, %s, %s, %s, %s, TRUE, %s)
        """, (
            context.user_data["quiz_question"],
            context.user_data["quiz_1"],
            context.user_data["quiz_2"],
            context.user_data["quiz_3"],
            context.user_data["quiz_4"],
            correct - 1,
            time.time()
        ))

        conn.commit()

        await update.message.reply_text(
            "✅ سوال با موفقیت اضافه شد!"
        )

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "ADD QUESTION ERROR"
        )

        await update.message.reply_text(
            "❌ خطا در ذخیره سوال."
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)

    context.user_data.clear()

    return ConversationHandler.END


async def addquestion_cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.user_data.clear()

    if update.message:

        await update.message.reply_text(
            "❌ افزودن سوال لغو شد."
        )

    return ConversationHandler.END


# =========================================================
# MARKET HELPERS
# =========================================================

def get_market():

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                name,
                price,
                group_id,
                updated_at
            FROM angrycoin_market
            WHERE id = 1
        """)

        row = cur.fetchone()

        if not row:
            return None

        return {
            "name": row[0],
            "price": float(row[1]),
            "group_id": row[2],
            "updated_at": row[3]
        }

    finally:

        if cur:
            cur.close()

        release_db(conn)


def get_holding(user_id):

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT amount
            FROM angrycoin_holdings
            WHERE user_id = %s
        """, (
            user_id,
        ))

        row = cur.fetchone()

        return (
            float(row[0])
            if row
            else 0
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)


def market_group_allowed(chat_id):

    market = get_market()

    if not market:
        return False

    group_id = market["group_id"]

    if group_id is None:
        return True

    return chat_id == group_id


# =========================================================
# MARKET COMMAND
# =========================================================

async def market(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.effective_chat:
        return

    if not market_group_allowed(
        update.effective_chat.id
    ):

        await update.message.reply_text(
            "📈 بورس AngryCoin در این گروه فعال نیست."
        )

        return

    data = get_market()

    if not data:
        return

    holding = get_holding(
        update.effective_user.id
    )

    value = (
        holding *
        data["price"]
    )

    await update.message.reply_text(
        f"📈 بورس AngryCoin\n\n"
        f"🪙 سهم: {data['name']}\n"
        f"💵 قیمت هر سهم: {data['price']:,.2f} 🪙\n\n"
        f"📦 دارایی شما: {holding:g} سهم\n"
        f"💰 ارزش دارایی: {value:,.2f} کوین\n\n"
        f"🛒 خرید:\n"
        f"/buy 1\n\n"
        f"📉 فروش:\n"
        f"/sell 1"
    )


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

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            UPDATE angrycoin_market
            SET group_id = %s,
                updated_at = %s
            WHERE id = 1
        """, (
            update.message.chat.id,
            time.time()
        ))

        conn.commit()

        await update.message.reply_text(
            "✅ بورس AngryCoin برای این گروه فعال شد."
        )

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "SET MARKET GROUP ERROR"
        )

        await update.message.reply_text(
            "❌ خطا."
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
            "❌ قیمت باید عدد باشد."
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

        cur.execute("""
            UPDATE angrycoin_market
            SET price = %s,
                updated_at = %s
            WHERE id = 1
        """, (
            price,
            time.time()
        ))

        cur.execute("""
            INSERT INTO angrycoin_history
            (
                price,
                created_at
            )
            VALUES
            (%s, %s)
        """, (
            price,
            time.time()
        ))

        conn.commit()

        await update.message.reply_text(
            f"✅ قیمت AngryCoin شد:\n\n"
            f"🪙 {price:,.2f} کوین"
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
# BUY ANGRYCOIN
# =========================================================

async def buy(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.effective_user:
        return

    if not update.effective_chat:
        return

    if not market_group_allowed(
        update.effective_chat.id
    ):

        await update.message.reply_text(
            "📈 بورس در این گروه فعال نیست."
        )

        return

    if not context.args:

        await update.message.reply_text(
            "مثال:\n/buy 2"
        )

        return

    try:

        amount = float(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ مقدار نامعتبر."
        )

        return

    if amount <= 0:

        await update.message.reply_text(
            "❌ مقدار باید بیشتر از صفر باشد."
        )

        return

    user = update.effective_user
    name = user.first_name or "کاربر"

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        # قفل کاربر
        cur.execute("""
            SELECT coins
            FROM users
            WHERE user_id = %s
            FOR UPDATE
        """, (
            user.id,
        ))

        row = cur.fetchone()

        coins = (
            int(row[0])
            if row
            else 0
        )

        cur.execute("""
            SELECT price
            FROM angrycoin_market
            WHERE id = 1
        """)

        market_row = cur.fetchone()

        if not market_row:
            raise Exception(
                "Market not found"
            )

        price = float(
            market_row[0]
        )

        cost = amount * price

        if cost > coins:

            await update.message.reply_text(
                f"❌ موجودی کافی نیست.\n\n"
                f"💰 هزینه: {cost:,.2f}\n"
                f"🪙 موجودی: {coins:,}"
            )

            conn.rollback()
            return

        # کم کردن کوین
        cur.execute("""
            INSERT INTO users
            (
                user_id,
                name,
                coins,
                last_message
            )
            VALUES
            (%s, %s, 0, 0)

            ON CONFLICT (user_id)
            DO UPDATE SET
                name = EXCLUDED.name
        """, (
            user.id,
            name
        ))

        cur.execute("""
            UPDATE users
            SET coins = coins - %s
            WHERE user_id = %s
        """, (
            int(round(cost)),
            user.id
        ))

        # اضافه کردن سهم
        cur.execute("""
            INSERT INTO angrycoin_holdings
            (
                user_id,
                amount
            )
            VALUES
            (%s, %s)

            ON CONFLICT (user_id)
            DO UPDATE SET
                amount =
                    angrycoin_holdings.amount
                    + EXCLUDED.amount
        """, (
            user.id,
            amount
        ))

        conn.commit()

        await update.message.reply_text(
            f"✅ خرید انجام شد!\n\n"
            f"🪙 AngryCoin: {amount:g} سهم\n"
            f"💵 قیمت: {price:,.2f}\n"
            f"💰 هزینه: {cost:,.2f} کوین"
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
# SELL ANGRYCOIN
# =========================================================

async def sell(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.effective_user:
        return

    if not update.effective_chat:
        return

    if not market_group_allowed(
        update.effective_chat.id
    ):

        await update.message.reply_text(
            "📈 بورس در این گروه فعال نیست."
        )

        return

    if not context.args:

        await update.message.reply_text(
            "مثال:\n/sell 2"
        )

        return

    try:

        amount = float(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ مقدار نامعتبر."
        )

        return

    if amount <= 0:
        return

    user = update.effective_user

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT amount
            FROM angrycoin_holdings
            WHERE user_id = %s
            FOR UPDATE
        """, (
            user.id,
        ))

        row = cur.fetchone()

        owned = (
            float(row[0])
            if row
            else 0
        )

        if amount > owned:

            await update.message.reply_text(
                f"❌ سهم کافی نداری.\n\n"
                f"📦 دارایی: {owned:g}"
            )

            conn.rollback()
            return

        cur.execute("""
            SELECT price
            FROM angrycoin_market
            WHERE id = 1
        """)

        price = float(
            cur.fetchone()[0]
        )

        revenue = amount * price

        cur.execute("""
            UPDATE angrycoin_holdings
            SET amount = amount - %s
            WHERE user_id = %s
        """, (
            amount,
            user.id
        ))

        cur.execute("""
            UPDATE users
            SET coins = coins + %s
            WHERE user_id = %s
        """, (
            int(round(revenue)),
            user.id
        ))

        conn.commit()

        await update.message.reply_text(
            f"✅ فروش انجام شد!\n\n"
            f"📉 AngryCoin: {amount:g} سهم\n"
            f"💵 قیمت: {price:,.2f}\n"
            f"💰 دریافتی: {revenue:,.2f} کوین"
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

    market_data = get_market()

    if not market_data:
        return

    holding = get_holding(
        update.effective_user.id
    )

    value = (
        holding *
        market_data["price"]
    )

    coins, _ = get_user(
        update.effective_user.id,
        update.effective_user.first_name or "کاربر"
    )

    await update.message.reply_text(
        f"📦 پرتفوی شما\n\n"
        f"🪙 AngryCoin: {holding:g} سهم\n"
        f"💵 قیمت فعلی: {market_data['price']:,.2f}\n"
        f"💰 ارزش سهام: {value:,.2f} کوین\n"
        f"💳 پول نقد: {coins:,} کوین\n\n"
        f"📊 ارزش کل: "
        f"{coins + value:,.2f} کوین"
    )


# =========================================================
# WEB APP GAME DATA
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
            data.get("score", 0)
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

    coins_awarded = (
        score *
        COINS_PER_GAME_SCORE
    )

    conn = None
    cur = None

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT 1
            FROM game_results
            WHERE game_id = %s
        """, (
            game_id,
        ))

        if cur.fetchone():

            conn.rollback()

            await message.reply_text(
                "⚠️ این بازی قبلاً ثبت شده."
            )

            return

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
            user.id,
            score,
            coins_awarded,
            time.time()
        ))

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
                        EXCLUDED.best_score
                    )
        """, (
            user.id,
            user.first_name or "کاربر",
            score,
            score
        ))

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
                    users.coins
                    + EXCLUDED.coins
        """, (
            user.id,
            user.first_name or "کاربر",
            coins_awarded
        ))

        conn.commit()

        await message.reply_text(
            f"🎮 بازی تموم شد!\n\n"
            f"🏆 امتیاز: {score}\n"
            f"🪙 جایزه: +{coins_awarded} کوین"
        )

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "GAME DATA ERROR"
        )

        await message.reply_text(
            "❌ خطا در ثبت بازی."
        )

    finally:

        if cur:
            cur.close()

        release_db(conn)


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
    })


@web.get("/health")
def health():

    return jsonify({
        "ok": True,
        "status": "healthy"
    })


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


# =========================================================
# GAME API
# =========================================================

@web.post("/game-score")
def game_score_api():

    data = request.get_json(
        silent=True
    ) or {}

    try:

        user_id = int(
            data.get("user_id", 0)
        )

        score = int(
            data.get("score", 0)
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

        cur.execute("""
            SELECT 1
            FROM game_results
            WHERE game_id = %s
        """, (
            game_id,
        ))

        if cur.fetchone():

            conn.rollback()

            return jsonify({
                "ok": False,
                "error": "game already registered"
            }), 409

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
                        EXCLUDED.best_score
                    )
        """, (
            user_id,
            name,
            score,
            score
        ))

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
                    users.coins
                    + EXCLUDED.coins
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
        })

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "GAME API ERROR"
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
# MARKET API
# =========================================================

@web.get("/market")
def market_api():

    try:

        data = get_market()

        return jsonify({
            "ok": True,
            "market": data
        })

    except Exception:

        logger.exception(
            "MARKET API ERROR"
        )

        return jsonify({
            "ok": False,
            "error": "market error"
        }), 500


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
        """, (
            user_id,
        ))

        row = cur.fetchone()

        return jsonify({
            "ok": True,
            "coins": (
                int(row[0])
                if row
                else 0
            )
        })

    finally:

        if cur:
            cur.close()

        release_db(conn)


@web.get("/portfolio/<int:user_id>")
def portfolio_api(user_id):

    market_data = get_market()

    holding = get_holding(
        user_id
    )

    value = (
        holding *
        market_data["price"]
    )

    return jsonify({
        "ok": True,
        "amount": holding,
        "price": market_data["price"],
        "value": value
    })


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
# TELEGRAM ERROR
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
        "Starting Telegram Bot..."
    )

    logger.info(
        "================================="
    )

    # -------------------------
    # DATABASE
    # -------------------------

    create_db_pool()
    init_db()

    # -------------------------
    # FLASK
    # -------------------------

    flask_thread = Thread(
        target=run_flask,
        daemon=True
    )

    flask_thread.start()

    # -------------------------
    # TELEGRAM
    # -------------------------

    application = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    # =====================================================
    # COMMANDS
    # =====================================================

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

    # =====================================================
    # QUIZ
    # =====================================================

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

    # =====================================================
    # MARKET
    # =====================================================

    application.add_handler(
        CommandHandler(
            "market",
            market
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
            "setprice",
            setprice
        )
    )

    application.add_handler(
        CommandHandler(
            "setmarketgroup",
            setmarketgroup
        )
    )

    # =====================================================
    # ADD QUESTION CONVERSATION
    # =====================================================

    add_question_conversation = (
        ConversationHandler(
            entry_points=[
                CommandHandler(
                    "addquestion",
                    addquestion_start
                )
            ],

            states={

                ADD_Q_TEXT: [
                    MessageHandler(
                        filters.TEXT
                        & ~filters.COMMAND,
                        addquestion_text
                    )
                ],

                ADD_Q_1: [
                    MessageHandler(
                        filters.TEXT
                        & ~filters.COMMAND,
                        addquestion_1
                    )
                ],

                ADD_Q_2: [
                    MessageHandler(
                        filters.TEXT
                        & ~filters.COMMAND,
                        addquestion_2
                    )
                ],

                ADD_Q_3: [
                    MessageHandler(
                        filters.TEXT
                        & ~filters.COMMAND,
                        addquestion_3
                    )
                ],

                ADD_Q_4: [
                    MessageHandler(
                        filters.TEXT
                        & ~filters.COMMAND,
                        addquestion_4
                    )
                ],

                ADD_Q_CORRECT: [
                    MessageHandler(
                        filters.TEXT
                        & ~filters.COMMAND,
                        addquestion_correct
                    )
                ],
            },

            fallbacks=[
                CommandHandler(
                    "cancel",
                    addquestion_cancel
                )
            ],
        )
    )

    application.add_handler(
        add_question_conversation
    )

    # =====================================================
    # POLL ANSWER
    # =====================================================

    application.add_handler(
        PollAnswerHandler(
            quiz_answer
        )
    )

    # =====================================================
    # WEB APP
    # =====================================================

    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.WEB_APP_DATA,
            web_app_data
        )
    )

    # =====================================================
    # TEXT
    # =====================================================

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            message_handler
        )
    )

    # =====================================================
    # ERROR HANDLER
    # =====================================================

    application.add_error_handler(
        telegram_error_handler
    )

    # =====================================================
    # START
    # =====================================================

    logger.info(
        "Bot is starting polling..."
    )

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":
    main()