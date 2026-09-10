import os
import time
import logging
from threading import Thread

import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from flask import Flask, request, jsonify

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

COINS_PER_MESSAGE = 10
COOLDOWN = 2 * 60

QUIZ_REWARD = 10
QUIZ_COOLDOWN = 30 * 60

DEFAULT_ANGRYCOIN_PRICE = 100

GAME_COIN_MULTIPLIER = 5


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)


# =========================================================
# FLASK
# =========================================================

web = Flask(__name__)


@web.route("/")
def home():
    return jsonify({
        "status": "online",
        "bot": "coin bot",
        "market": "AngryCoin",
        "game": "Subway Bird"
    })


@web.route("/health")
def health():
    return jsonify({
        "status": "ok"
    })


# =========================================================
# DATABASE
# =========================================================

db_pool = ThreadedConnectionPool(
    minconn=1,
    maxconn=10,
    dsn=DATABASE_URL,
    connect_timeout=10,
    sslmode="require"
)


def get_conn():
    return db_pool.getconn()


def put_conn(conn):
    db_pool.putconn(conn)


def init_db():

    conn = get_conn()

    try:

        cur = conn.cursor()

        # =====================================================
        # USERS
        # =====================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                coins BIGINT DEFAULT 0,
                total_coins BIGINT DEFAULT 0,
                last_message DOUBLE PRECISION DEFAULT 0
            )
        """)

        # =====================================================
        # GROUPS
        # =====================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_groups (
                chat_id BIGINT PRIMARY KEY,
                title TEXT
            )
        """)

        # =====================================================
        # QUIZ QUESTIONS
        # =====================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS quiz_questions (
                id SERIAL PRIMARY KEY,
                question TEXT NOT NULL,
                options TEXT[] NOT NULL,
                correct_index INTEGER NOT NULL,
                enabled BOOLEAN DEFAULT TRUE
            )
        """)

        # =====================================================
        # QUIZ POLLS
        # =====================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS quiz_polls (
                poll_id TEXT PRIMARY KEY,
                question_id INTEGER,
                correct_index INTEGER
            )
        """)

        # =====================================================
        # QUIZ ANSWERS
        # =====================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS quiz_answers (
                poll_id TEXT,
                user_id BIGINT,
                answered_at DOUBLE PRECISION,
                PRIMARY KEY (poll_id, user_id)
            )
        """)

        # =====================================================
        # QUIZ COOLDOWNS
        # =====================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS quiz_cooldowns (
                chat_id BIGINT PRIMARY KEY,
                last_quiz DOUBLE PRECISION DEFAULT 0
            )
        """)

        # =====================================================
        # QUIZ USER STATS
        # =====================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS quiz_user_stats (
                user_id BIGINT PRIMARY KEY,
                correct INTEGER DEFAULT 0,
                wrong INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0
            )
        """)

        # =====================================================
        # MARKET
        # =====================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS market (
                symbol TEXT PRIMARY KEY,
                name TEXT,
                price BIGINT DEFAULT 100
            )
        """)

        # =====================================================
        # MARKET HOLDINGS
        # =====================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_holdings (
                user_id BIGINT,
                symbol TEXT,
                amount BIGINT DEFAULT 0,
                PRIMARY KEY (user_id, symbol)
            )
        """)

        # =====================================================
        # MARKET HISTORY
        # =====================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_history (
                id SERIAL PRIMARY KEY,
                symbol TEXT,
                price BIGINT,
                created_at DOUBLE PRECISION
            )
        """)

        # =====================================================
        # MARKET GROUPS
        # =====================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_groups (
                chat_id BIGINT PRIMARY KEY
            )
        """)

        # =====================================================
        # GAME SCORES
        # =====================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS game_scores (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                username TEXT,
                name TEXT,
                game_id TEXT,
                score INTEGER DEFAULT 0,
                coins_awarded INTEGER DEFAULT 0,
                created_at DOUBLE PRECISION
            )
        """)

        # =====================================================
        # GAME RESULTS
        # =====================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS game_results (
                user_id BIGINT PRIMARY KEY,
                best_score INTEGER DEFAULT 0,
                total_score BIGINT DEFAULT 0,
                games_played INTEGER DEFAULT 0
            )
        """)

        # =====================================================
        # DEFAULT MARKET
        # =====================================================

        cur.execute("""
            INSERT INTO market (
                symbol,
                name,
                price
            )
            VALUES (
                'ANGRYCOIN',
                'AngryCoin',
                %s
            )
            ON CONFLICT (symbol) DO NOTHING
        """, (
            DEFAULT_ANGRYCOIN_PRICE,
        ))

        conn.commit()
        cur.close()

        logger.info("Database initialized.")

    except Exception:

        conn.rollback()

        logger.exception(
            "Database initialization error"
        )

    finally:

        put_conn(conn)


# =========================================================
# USER FUNCTIONS
# =========================================================

def ensure_user(user):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            INSERT INTO users (
                user_id,
                username,
                first_name
            )
            VALUES (
                %s,
                %s,
                %s
            )

            ON CONFLICT (user_id)
            DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name
        """, (
            user.id,
            user.username,
            user.first_name
        ))

        conn.commit()
        cur.close()

    finally:

        put_conn(conn)


def get_balance(user_id):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT coins
            FROM users
            WHERE user_id = %s
        """, (
            user_id,
        ))

        row = cur.fetchone()

        cur.close()

        return row[0] if row else 0

    finally:

        put_conn(conn)


def add_coins(user_id, amount):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            INSERT INTO users (
                user_id,
                coins,
                total_coins
            )
            VALUES (
                %s,
                %s,
                %s
            )

            ON CONFLICT (user_id)
            DO UPDATE SET
                coins =
                    users.coins + EXCLUDED.coins,

                total_coins =
                    users.total_coins + EXCLUDED.total_coins
        """, (
            user_id,
            amount,
            amount
        ))

        conn.commit()
        cur.close()

    finally:

        put_conn(conn)


def remove_coins(user_id, amount):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            UPDATE users
            SET coins = GREATEST(
                coins - %s,
                0
            )
            WHERE user_id = %s
        """, (
            amount,
            user_id
        ))

        conn.commit()
        cur.close()

    finally:

        put_conn(conn)


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if user:
        ensure_user(user)

    await update.message.reply_text(
        "🐦 سلام!\n\n"
        "🪙 ربات کوین فعاله.\n\n"
        "💰 /balance\n"
        "🏆 /top\n"
        "🧠 /quiz\n"
        "📈 /market\n"
        "🎮 /gamestats"
    )


# =========================================================
# MESSAGE COINS
# =========================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    text = update.message.text or ""

    if text.strip() not in [
        "فولک",
        "هاپهاپ کوین"
    ]:
        return

    now = time.time()

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT last_message
            FROM users
            WHERE user_id = %s
        """, (
            user.id,
        ))

        row = cur.fetchone()

        last = row[0] if row else 0

        if now - last >= COOLDOWN:

            cur.execute("""
                UPDATE users
                SET
                    coins = coins + %s,
                    total_coins = total_coins + %s,
                    last_message = %s
                WHERE user_id = %s
            """, (
                COINS_PER_MESSAGE,
                COINS_PER_MESSAGE,
                now,
                user.id
            ))

            conn.commit()

            await update.message.reply_text(
                f"🪙 +{COINS_PER_MESSAGE} کوین گرفتی!"
            )

        else:

            remaining = int(
                COOLDOWN -
                (now - last)
            )

            minutes = remaining // 60
            seconds = remaining % 60

            await update.message.reply_text(
                f"⏳ هنوز زوده!\n"
                f"{minutes} دقیقه و "
                f"{seconds} ثانیه دیگه امتحان کن."
            )

        cur.close()

    finally:

        put_conn(conn)


# =========================================================
# BALANCE
# =========================================================

async def balance(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    ensure_user(user)

    coins = get_balance(user.id)

    await update.message.reply_text(
        f"💰 موجودی شما:\n\n"
        f"🪙 {coins} کوین"
    )


# =========================================================
# TOP
# =========================================================

async def top(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT
                first_name,
                username,
                coins
            FROM users
            ORDER BY coins DESC
            LIMIT 10
        """)

        rows = cur.fetchall()

        cur.close()

    finally:

        put_conn(conn)

    if not rows:

        await update.message.reply_text(
            "هنوز کسی کوین نداره 😅"
        )

        return

    text = "🏆 TOP 10\n\n"

    for i, row in enumerate(rows, 1):

        name = (
            row[0]
            or row[1]
            or "Unknown"
        )

        coins = row[2]

        text += (
            f"{i}. {name} — "
            f"🪙 {coins}\n"
        )

    await update.message.reply_text(text)


# =========================================================
# ADMIN CHECK
# =========================================================

def is_admin(user_id):
    return user_id == ADMIN_ID


# =========================================================
# ADD COINS
# =========================================================

async def addcoins_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    if not update.message.reply_to_message:

        await update.message.reply_text(
            "❌ روی پیام کاربر Reply کن:\n"
            "/addcoins 100"
        )

        return

    if not context.args:

        await update.message.reply_text(
            "❌ مقدار کوین رو وارد کن."
        )

        return

    try:

        amount = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ مقدار باید عدد باشه."
        )

        return

    if amount <= 0:

        await update.message.reply_text(
            "❌ مقدار باید بیشتر از صفر باشه."
        )

        return

    target = (
        update.message
        .reply_to_message
        .from_user
    )

    ensure_user(target)

    add_coins(
        target.id,
        amount
    )

    await update.message.reply_text(
        f"✅ {amount} کوین به "
        f"{target.first_name} اضافه شد."
    )


# =========================================================
# REMOVE COINS
# =========================================================

async def removecoins_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    if not update.message.reply_to_message:

        await update.message.reply_text(
            "❌ روی پیام کاربر Reply کن."
        )

        return

    if not context.args:

        await update.message.reply_text(
            "❌ مقدار کوین رو وارد کن."
        )

        return

    try:

        amount = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ مقدار باید عدد باشه."
        )

        return

    if amount <= 0:

        await update.message.reply_text(
            "❌ مقدار باید بیشتر از صفر باشه."
        )

        return

    target = (
        update.message
        .reply_to_message
        .from_user
    )

    ensure_user(target)

    remove_coins(
        target.id,
        amount
    )

    await update.message.reply_text(
        f"✅ {amount} کوین از "
        f"{target.first_name} کم شد."
    )


# =========================================================
# ADD ALL
# =========================================================

async def addall(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    if not context.args:

        await update.message.reply_text(
            "مثال:\n"
            "/addall 100"
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

        await update.message.reply_text(
            "❌ مقدار باید بیشتر از صفر باشد."
        )

        return

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            UPDATE users
            SET
                coins = coins + %s,
                total_coins = total_coins + %s
        """, (
            amount,
            amount
        ))

        count = cur.rowcount

        conn.commit()

        cur.close()

    except Exception:

        conn.rollback()
        raise

    finally:

        put_conn(conn)

    await update.message.reply_text(
        f"✅ به {count} کاربر، "
        f"نفری {amount} کوین اضافه شد."
    )


# =========================================================
# PLAYER STATS
# =========================================================

async def playerstats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    target = None

    if update.message.reply_to_message:

        target = (
            update.message
            .reply_to_message
            .from_user
        )

    elif context.args:

        try:
            target_id = int(
                context.args[0]
            )
        except ValueError:

            await update.message.reply_text(
                "❌ ID باید عدد باشد."
            )

            return

        conn = get_conn()

        try:

            cur = conn.cursor()

            cur.execute("""
                SELECT
                    user_id,
                    first_name,
                    username,
                    coins,
                    total_coins
                FROM users
                WHERE user_id = %s
            """, (
                target_id,
            ))

            row = cur.fetchone()

            cur.execute("""
                SELECT
                    best_score,
                    total_score,
                    games_played
                FROM game_results
                WHERE user_id = %s
            """, (
                target_id,
            ))

            game_row = cur.fetchone()

            cur.close()

        finally:

            put_conn(conn)

        if not row:

            await update.message.reply_text(
                "❌ کاربر پیدا نشد."
            )

            return

        (
            user_id,
            first_name,
            username,
            coins,
            total_coins
        ) = row

    else:

        await update.message.reply_text(
            "❌ روی پیام کاربر Reply کن یا ID بده."
        )

        return

    if target:

        ensure_user(target)

        user_id = target.id
        first_name = target.first_name
        username = target.username
        coins = get_balance(target.id)

        conn = get_conn()

        try:

            cur = conn.cursor()

            cur.execute("""
                SELECT total_coins
                FROM users
                WHERE user_id = %s
            """, (
                target.id,
            ))

            row = cur.fetchone()

            total_coins = row[0] if row else 0

            cur.execute("""
                SELECT
                    best_score,
                    total_score,
                    games_played
                FROM game_results
                WHERE user_id = %s
            """, (
                target.id,
            ))

            game_row = cur.fetchone()

            cur.close()

        finally:

            put_conn(conn)

    if game_row:

        best_score = game_row[0]
        total_score = game_row[1]
        games_played = game_row[2]

    else:

        best_score = 0
        total_score = 0
        games_played = 0

    await update.message.reply_text(
        f"👤 Player Stats\n\n"
        f"ID: {user_id}\n"
        f"Name: {first_name}\n"
        f"Username: @{username if username else '---'}\n\n"
        f"💰 Balance: {coins}\n"
        f"📊 Total Coins: {total_coins}\n\n"
        f"🎮 Game Stats\n"
        f"🏆 Best Score: {best_score}\n"
        f"📈 Total Score: {total_score}\n"
        f"🎮 Games Played: {games_played}"
    )


# =========================================================
# SAY
# =========================================================

async def say(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):
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

    if not is_admin(
        update.effective_user.id
    ):
        return

    chat = update.effective_chat

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            INSERT INTO bot_groups (
                chat_id,
                title
            )
            VALUES (
                %s,
                %s
            )
            ON CONFLICT (chat_id)
            DO UPDATE SET
                title = EXCLUDED.title
        """, (
            chat.id,
            chat.title or ""
        ))

        conn.commit()
        cur.close()

    finally:

        put_conn(conn)

    await update.message.reply_text(
        "✅ این گروه به عنوان گروه ربات ثبت شد."
    )


# =========================================================
# GROUP MESSAGE
# =========================================================

async def groupmsg(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    if not context.args:

        await update.message.reply_text(
            "متن پیام رو بنویس."
        )

        return

    message = " ".join(
        context.args
    )

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT chat_id
            FROM bot_groups
        """)

        groups = cur.fetchall()

        cur.close()

    finally:

        put_conn(conn)

    sent = 0

    for row in groups:

        try:

            await context.bot.send_message(
                chat_id=row[0],
                text=message
            )

            sent += 1

        except Exception as e:

            logger.warning(
                "Could not send group message: %s",
                e
            )

    await update.message.reply_text(
        f"📢 پیام به {sent} گروه ارسال شد."
    )


# =========================================================
# QUIZ
# =========================================================

async def quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = update.effective_chat.id
    now = time.time()

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT last_quiz
            FROM quiz_cooldowns
            WHERE chat_id = %s
        """, (
            chat_id,
        ))

        row = cur.fetchone()

        last = row[0] if row else 0

        if now - last < QUIZ_COOLDOWN:

            remaining = int(
                QUIZ_COOLDOWN -
                (now - last)
            )

            minutes = remaining // 60
            seconds = remaining % 60

            cur.close()

            await update.message.reply_text(
                f"⏳ سوال بعدی تا "
                f"{minutes}:{seconds:02d}"
            )

            return

        cur.execute("""
            SELECT
                id,
                question,
                options,
                correct_index
            FROM quiz_questions
            WHERE enabled = TRUE
            ORDER BY RANDOM()
            LIMIT 1
        """)

        question = cur.fetchone()

        if not question:

            cur.close()

            await update.message.reply_text(
                "❌ هنوز سوالی ثبت نشده."
            )

            return

        (
            question_id,
            text,
            options,
            correct_index
        ) = question

        cur.execute("""
            INSERT INTO quiz_cooldowns (
                chat_id,
                last_quiz
            )
            VALUES (
                %s,
                %s
            )
            ON CONFLICT (chat_id)
            DO UPDATE SET
                last_quiz = EXCLUDED.last_quiz
        """, (
            chat_id,
            now
        ))

        conn.commit()
        cur.close()

    finally:

        put_conn(conn)

    try:

        message = await context.bot.send_poll(
            chat_id=chat_id,
            question=text,
            options=options,
            type=Poll.QUIZ,
            correct_option_id=correct_index,
            is_anonymous=False
        )

    except Exception:

        # اگر ارسال سوال شکست خورد،
        # cooldown را برمی‌گردانیم.
        conn = get_conn()

        try:

            cur = conn.cursor()

            cur.execute("""
                DELETE FROM quiz_cooldowns
                WHERE chat_id = %s
                AND last_quiz = %s
            """, (
                chat_id,
                now
            ))

            conn.commit()
            cur.close()

        finally:

            put_conn(conn)

        raise

    poll_id = message.poll.id

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            INSERT INTO quiz_polls (
                poll_id,
                question_id,
                correct_index
            )
            VALUES (
                %s,
                %s,
                %s
            )
            ON CONFLICT (poll_id) DO NOTHING
        """, (
            poll_id,
            question_id,
            correct_index
        ))

        conn.commit()
        cur.close()

    finally:

        put_conn(conn)


# =========================================================
# POLL ANSWER
# =========================================================

async def poll_answer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    answer = update.poll_answer

    poll_id = answer.poll_id
    user = answer.user

    if not answer.option_ids:
        return

    selected = answer.option_ids[0]

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT correct_index
            FROM quiz_polls
            WHERE poll_id = %s
        """, (
            poll_id,
        ))

        row = cur.fetchone()

        if not row:

            cur.close()
            return

        correct_index = row[0]

        cur.execute("""
            INSERT INTO quiz_answers (
                poll_id,
                user_id,
                answered_at
            )
            VALUES (
                %s,
                %s,
                %s
            )
            ON CONFLICT DO NOTHING
        """, (
            poll_id,
            user.id,
            time.time()
        ))

        # اگر قبلاً جواب داده، دوباره جایزه نده
        if cur.rowcount == 0:

            conn.commit()
            cur.close()
            return

        if selected == correct_index:

            cur.execute("""
                INSERT INTO users (
                    user_id,
                    username,
                    first_name,
                    coins,
                    total_coins
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
                ON CONFLICT (user_id)
                DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    coins =
                        users.coins +
                        EXCLUDED.coins,
                    total_coins =
                        users.total_coins +
                        EXCLUDED.total_coins
            """, (
                user.id,
                user.username,
                user.first_name,
                QUIZ_REWARD,
                QUIZ_REWARD
            ))

            cur.execute("""
                INSERT INTO quiz_user_stats (
                    user_id,
                    correct,
                    wrong,
                    total
                )
                VALUES (
                    %s,
                    1,
                    0,
                    1
                )
                ON CONFLICT (user_id)
                DO UPDATE SET
                    correct =
                        quiz_user_stats.correct + 1,
                    total =
                        quiz_user_stats.total + 1
            """, (
                user.id,
            ))

        else:

            cur.execute("""
                INSERT INTO quiz_user_stats (
                    user_id,
                    correct,
                    wrong,
                    total
                )
                VALUES (
                    %s,
                    0,
                    1,
                    1
                )
                ON CONFLICT (user_id)
                DO UPDATE SET
                    wrong =
                        quiz_user_stats.wrong + 1,
                    total =
                        quiz_user_stats.total + 1
            """, (
                user.id,
            ))

        conn.commit()
        cur.close()

    except Exception:

        conn.rollback()

        logger.exception(
            "Poll answer error"
        )

    finally:

        put_conn(conn)


# =========================================================
# QUIZ SCORE
# =========================================================

async def quizscore(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT
                correct,
                wrong,
                total
            FROM quiz_user_stats
            WHERE user_id = %s
        """, (
            user.id,
        ))

        row = cur.fetchone()

        cur.close()

    finally:

        put_conn(conn)

    if not row:

        await update.message.reply_text(
            "هنوز در هیچ کوییزی شرکت نکردی."
        )

        return

    correct, wrong, total = row

    await update.message.reply_text(
        f"🧠 Quiz Stats\n\n"
        f"✅ درست: {correct}\n"
        f"❌ غلط: {wrong}\n"
        f"📊 مجموع: {total}"
    )


# =========================================================
# QUIZ TOP
# =========================================================

async def quiztop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT
                u.first_name,
                u.username,
                q.correct,
                q.total
            FROM quiz_user_stats q
            LEFT JOIN users u
                ON u.user_id = q.user_id
            ORDER BY q.correct DESC
            LIMIT 10
        """)

        rows = cur.fetchall()

        cur.close()

    finally:

        put_conn(conn)

    if not rows:

        await update.message.reply_text(
            "هنوز کسی در کوئیز شرکت نکرده."
        )

        return

    text = "🏆 Quiz TOP\n\n"

    for i, row in enumerate(rows, 1):

        name = (
            row[0]
            or row[1]
            or "Unknown"
        )

        text += (
            f"{i}. {name} — "
            f"✅ {row[2]} / {row[3]}\n"
        )

    await update.message.reply_text(text)


# =========================================================
# ADD QUESTION
# =========================================================

async def addquestion(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    raw = update.message.text or ""

    if raw.startswith("/addquestion"):

        raw = raw[
            len("/addquestion"):
        ].strip()

    if not raw:

        await update.message.reply_text(
            "فرمت:\n\n"
            "/addquestion سوال | گزینه1 | گزینه2 | "
            "گزینه3 | گزینه4 | شماره جواب درست\n\n"
            "مثال:\n"
            "/addquestion پایتخت ایران چیست؟ | "
            "تهران | شیراز | تبریز | اهواز | 1"
        )

        return

    parts = [
        x.strip()
        for x in raw.split("|")
    ]

    if len(parts) != 6:

        await update.message.reply_text(
            "❌ فرمت اشتباهه.\n\n"
            "باید دقیقاً این شکلی باشه:\n"
            "/addquestion سوال | گزینه1 | گزینه2 | "
            "گزینه3 | گزینه4 | شماره جواب درست"
        )

        return

    question = parts[0]
    options = parts[1:5]

    try:

        correct_index = int(parts[5]) - 1

    except ValueError:

        await update.message.reply_text(
            "❌ شماره جواب درست باید 1 تا 4 باشد."
        )

        return

    if not question:

        await update.message.reply_text(
            "❌ متن سوال خالیه."
        )

        return

    if any(not option for option in options):

        await update.message.reply_text(
            "❌ گزینه‌ها نباید خالی باشند."
        )

        return

    if correct_index not in range(4):

        await update.message.reply_text(
            "❌ شماره جواب درست باید بین 1 تا 4 باشد."
        )

        return

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            INSERT INTO quiz_questions (
                question,
                options,
                correct_index,
                enabled
            )
            VALUES (
                %s,
                %s,
                %s,
                TRUE
            )
            RETURNING id
        """, (
            question,
            options,
            correct_index
        ))

        question_id = cur.fetchone()[0]

        conn.commit()
        cur.close()

    except Exception:

        conn.rollback()

        logger.exception(
            "Add question error"
        )

        await update.message.reply_text(
            "❌ خطا در ذخیره سوال."
        )

        return

    finally:

        put_conn(conn)

    await update.message.reply_text(
        f"✅ سوال با ID {question_id} اضافه شد."
    )


# =========================================================
# QUESTIONS
# =========================================================

async def questions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT
                id,
                question,
                enabled
            FROM quiz_questions
            ORDER BY id
        """)

        rows = cur.fetchall()

        cur.close()

    finally:

        put_conn(conn)

    if not rows:

        await update.message.reply_text(
            "هیچ سوالی وجود نداره."
        )

        return

    text = "📚 Questions\n\n"

    for row in rows:

        status = (
            "🟢"
            if row[2]
            else "🔴"
        )

        text += (
            f"{row[0]}. {status} "
            f"{row[1]}\n"
        )

    await update.message.reply_text(text)


# =========================================================
# DELETE QUESTION
# =========================================================

async def delquestion(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    if not context.args:

        await update.message.reply_text(
            "/delquestion 1"
        )

        return

    try:

        qid = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ ID باید عدد باشد."
        )

        return

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            DELETE FROM quiz_questions
            WHERE id = %s
        """, (
            qid,
        ))

        deleted = cur.rowcount

        conn.commit()
        cur.close()

    finally:

        put_conn(conn)

    if deleted:

        await update.message.reply_text(
            f"🗑 سوال {qid} حذف شد."
        )

    else:

        await update.message.reply_text(
            "❌ سوال پیدا نشد."
        )


# =========================================================
# ENABLE QUESTION
# =========================================================

async def enablequestion(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    if not context.args:

        await update.message.reply_text(
            "/enablequestion 1"
        )

        return

    try:

        qid = int(
            context.args[0]
        )

    except ValueError:

        return

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            UPDATE quiz_questions
            SET enabled = TRUE
            WHERE id = %s
        """, (
            qid,
        ))

        changed = cur.rowcount

        conn.commit()
        cur.close()

    finally:

        put_conn(conn)

    await update.message.reply_text(
        f"🟢 سوال {qid} فعال شد."
        if changed
        else "❌ سوال پیدا نشد."
    )


# =========================================================
# DISABLE QUESTION
# =========================================================

async def disablequestion(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    if not context.args:

        await update.message.reply_text(
            "/disablequestion 1"
        )

        return

    try:

        qid = int(
            context.args[0]
        )

    except ValueError:

        return

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            UPDATE quiz_questions
            SET enabled = FALSE
            WHERE id = %s
        """, (
            qid,
        ))

        changed = cur.rowcount

        conn.commit()
        cur.close()

    finally:

        put_conn(conn)

    await update.message.reply_text(
        f"🔴 سوال {qid} غیرفعال شد."
        if changed
        else "❌ سوال پیدا نشد."
    )


# =========================================================
# MARKET
# =========================================================

async def market(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT
                symbol,
                name,
                price
            FROM market
            ORDER BY symbol
        """)

        rows = cur.fetchall()

        cur.close()

    finally:

        put_conn(conn)

    text = "📈 بازار AngryCoin\n\n"

    for symbol, name, price in rows:

        text += (
            f"🪙 {name}\n"
            f"Symbol: {symbol}\n"
            f"Price: {price}\n\n"
        )

    await update.message.reply_text(text)


# =========================================================
# SET PRICE
# =========================================================

async def setprice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    if len(context.args) < 2:

        await update.message.reply_text(
            "/setprice ANGRYCOIN 150"
        )

        return

    symbol = context.args[0].upper()

    try:

        price = int(
            context.args[1]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ قیمت باید عدد باشد."
        )

        return

    if price < 0:

        await update.message.reply_text(
            "❌ قیمت نمی‌تواند منفی باشد."
        )

        return

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            UPDATE market
            SET price = %s
            WHERE symbol = %s
        """, (
            price,
            symbol
        ))

        if cur.rowcount == 0:

            conn.rollback()
            cur.close()

            await update.message.reply_text(
                f"❌ سهم {symbol} وجود ندارد."
            )

            return

        cur.execute("""
            INSERT INTO market_history (
                symbol,
                price,
                created_at
            )
            VALUES (
                %s,
                %s,
                %s
            )
        """, (
            symbol,
            price,
            time.time()
        ))

        conn.commit()
        cur.close()

    finally:

        put_conn(conn)

    await update.message.reply_text(
        f"✅ قیمت {symbol} شد {price}"
    )


# =========================================================
# BUY
# =========================================================

async def buy(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    ensure_user(user)

    if not context.args:

        await update.message.reply_text(
            "/buy 10"
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

        await update.message.reply_text(
            "❌ مقدار باید بیشتر از صفر باشد."
        )

        return

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT price
            FROM market
            WHERE symbol = 'ANGRYCOIN'
        """)

        row = cur.fetchone()

        if not row:

            cur.close()

            await update.message.reply_text(
                "❌ بازار موجود نیست."
            )

            return

        price = row[0]

        total_cost = amount * price

        cur.execute("""
            SELECT coins
            FROM users
            WHERE user_id = %s
        """, (
            user.id,
        ))

        balance_row = cur.fetchone()

        balance_value = (
            balance_row[0]
            if balance_row
            else 0
        )

        if balance_value < total_cost:

            cur.close()

            await update.message.reply_text(
                f"❌ کوین کافی نداری.\n"
                f"هزینه: {total_cost}\n"
                f"موجودی: {balance_value}"
            )

            return

        cur.execute("""
            UPDATE users
            SET coins = coins - %s
            WHERE user_id = %s
        """, (
            total_cost,
            user.id
        ))

        cur.execute("""
            INSERT INTO market_holdings (
                user_id,
                symbol,
                amount
            )
            VALUES (
                %s,
                'ANGRYCOIN',
                %s
            )
            ON CONFLICT (
                user_id,
                symbol
            )
            DO UPDATE SET
                amount =
                    market_holdings.amount +
                    EXCLUDED.amount
        """, (
            user.id,
            amount
        ))

        conn.commit()
        cur.close()

    except Exception:

        conn.rollback()
        raise

    finally:

        put_conn(conn)

    await update.message.reply_text(
        f"✅ خرید انجام شد!\n\n"
        f"🪙 مقدار: {amount}\n"
        f"💰 هزینه: {total_cost}"
    )


# =========================================================
# SELL
# =========================================================

async def sell(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    ensure_user(user)

    if not context.args:

        await update.message.reply_text(
            "/sell 10"
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

        await update.message.reply_text(
            "❌ مقدار باید بیشتر از صفر باشد."
        )

        return

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT price
            FROM market
            WHERE symbol = 'ANGRYCOIN'
        """)

        price_row = cur.fetchone()

        if not price_row:

            cur.close()

            await update.message.reply_text(
                "❌ بازار موجود نیست."
            )

            return

        price = price_row[0]

        cur.execute("""
            SELECT amount
            FROM market_holdings
            WHERE user_id = %s
            AND symbol = 'ANGRYCOIN'
        """, (
            user.id,
        ))

        row = cur.fetchone()

        owned = (
            row[0]
            if row
            else 0
        )

        if owned < amount:

            cur.close()

            await update.message.reply_text(
                f"❌ این مقدار رو نداری.\n"
                f"موجودی سهم: {owned}"
            )

            return

        value = amount * price

        cur.execute("""
            UPDATE market_holdings
            SET amount = amount - %s
            WHERE user_id = %s
            AND symbol = 'ANGRYCOIN'
        """, (
            amount,
            user.id
        ))

        cur.execute("""
            UPDATE users
            SET coins = coins + %s
            WHERE user_id = %s
        """, (
            value,
            user.id
        ))

        conn.commit()
        cur.close()

    except Exception:

        conn.rollback()
        raise

    finally:

        put_conn(conn)

    await update.message.reply_text(
        f"✅ فروش انجام شد!\n\n"
        f"🪙 مقدار: {amount}\n"
        f"💰 دریافتی: {value}"
    )


# =========================================================
# PORTFOLIO
# =========================================================

async def portfolio(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    ensure_user(user)

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT
                h.symbol,
                h.amount,
                m.price
            FROM market_holdings h
            JOIN market m
                ON m.symbol = h.symbol
            WHERE h.user_id = %s
            AND h.amount > 0
        """, (
            user.id,
        ))

        rows = cur.fetchall()

        cur.close()

    finally:

        put_conn(conn)

    if not rows:

        await update.message.reply_text(
            "📊 پرتفوی شما خالیه."
        )

        return

    text = "📊 Portfolio\n\n"

    total = 0

    for symbol, amount, price in rows:

        value = amount * price

        total += value

        text += (
            f"🪙 {symbol}\n"
            f"تعداد: {amount}\n"
            f"قیمت: {price}\n"
            f"ارزش: {value}\n\n"
        )

    text += (
        f"💰 ارزش کل: {total}"
    )

    await update.message.reply_text(text)


# =========================================================
# MARKET GROUP
# =========================================================

async def setmarketgroup(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    chat_id = update.effective_chat.id

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            INSERT INTO market_groups (
                chat_id
            )
            VALUES (
                %s
            )
            ON CONFLICT DO NOTHING
        """, (
            chat_id,
        ))

        conn.commit()
        cur.close()

    finally:

        put_conn(conn)

    await update.message.reply_text(
        "📈 این گروه برای بازار ثبت شد."
    )


async def unsetmarketgroup(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    chat_id = update.effective_chat.id

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            DELETE FROM market_groups
            WHERE chat_id = %s
        """, (
            chat_id,
        ))

        conn.commit()
        cur.close()

    finally:

        put_conn(conn)

    await update.message.reply_text(
        "✅ گروه از بازار حذف شد."
    )


# =========================================================
# GAME SCORE API
# =========================================================

@web.route(
    "/game-score",
    methods=["POST"]
)
def game_score():

    try:

        data = request.get_json(
            silent=True
        )

        if not data:

            return jsonify({
                "ok": False,
                "error": "invalid_json"
            }), 400

        user_id = data.get("user_id")

        name = (
            str(data.get("name") or "Player")
            [:100]
        )

        game_id = (
            str(data.get("game_id") or "subway_bird")
            [:50]
        )

        score = data.get("score", 0)

        if user_id is None:

            return jsonify({
                "ok": False,
                "error": "missing_user_id"
            }), 400

        try:

            user_id = int(user_id)
            score = int(score)

        except (
            TypeError,
            ValueError
        ):

            return jsonify({
                "ok": False,
                "error": "invalid_values"
            }), 400

        if user_id <= 0:

            return jsonify({
                "ok": False,
                "error": "invalid_user_id"
            }), 400

        if score < 0:
            score = 0

        if score > 1000000:

            return jsonify({
                "ok": False,
                "error": "score_too_high"
            }), 400

        coins_awarded = (
            score * GAME_COIN_MULTIPLIER
        )

        conn = get_conn()

        try:

            cur = conn.cursor()

            # =================================================
            # GAME RESULT
            # =================================================

            cur.execute("""
                SELECT
                    best_score,
                    total_score,
                    games_played
                FROM game_results
                WHERE user_id = %s
            """, (
                user_id,
            ))

            old = cur.fetchone()

            if old:

                old_best, old_total, old_games = old

                new_best = max(
                    old_best,
                    score
                )

                new_total = (
                    old_total + score
                )

                new_games = (
                    old_games + 1
                )

            else:

                new_best = score
                new_total = score
                new_games = 1

            # =================================================
            # GAME RESULT UPDATE
            # =================================================

            cur.execute("""
                INSERT INTO game_results (
                    user_id,
                    best_score,
                    total_score,
                    games_played
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s
                )
                ON CONFLICT (user_id)
                DO UPDATE SET
                    best_score =
                        EXCLUDED.best_score,
                    total_score =
                        EXCLUDED.total_score,
                    games_played =
                        EXCLUDED.games_played
            """, (
                user_id,
                new_best,
                new_total,
                new_games
            ))

            # =================================================
            # SAVE GAME
            # =================================================

            cur.execute("""
                INSERT INTO game_scores (
                    user_id,
                    username,
                    name,
                    game_id,
                    score,
                    coins_awarded,
                    created_at
                )
                VALUES (
                    %s,
                    NULL,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
            """, (
                user_id,
                name,
                game_id,
                score,
                coins_awarded,
                time.time()
            ))

            # =================================================
            # GIVE COINS
            # =================================================

            cur.execute("""
                INSERT INTO users (
                    user_id,
                    first_name,
                    coins,
                    total_coins
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s
                )
                ON CONFLICT (user_id)
                DO UPDATE SET
                    first_name =
                        EXCLUDED.first_name,
                    coins =
                        users.coins +
                        EXCLUDED.coins,
                    total_coins =
                        users.total_coins +
                        EXCLUDED.total_coins
            """, (
                user_id,
                name,
                coins_awarded,
                coins_awarded
            ))

            conn.commit()
            cur.close()

        except Exception:

            conn.rollback()
            raise

        finally:

            put_conn(conn)

        return jsonify({
            "ok": True,
            "score": score,
            "coins_awarded": coins_awarded,
            "best_score": new_best,
            "total_score": new_total,
            "games_played": new_games
        })

    except Exception:

        logger.exception(
            "Game score error"
        )

        return jsonify({
            "ok": False,
            "error": "server_error"
        }), 500


# =========================================================
# GAME STATS
# =========================================================

async def gamestats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT
                best_score,
                total_score,
                games_played
            FROM game_results
            WHERE user_id = %s
        """, (
            user.id,
        ))

        row = cur.fetchone()

        cur.close()

    finally:

        put_conn(conn)

    if not row:

        await update.message.reply_text(
            "🎮 هنوز بازی نکردی!"
        )

        return

    best, total, games = row

    await update.message.reply_text(
        f"🎮 Game Stats\n\n"
        f"🏆 رکورد: {best}\n"
        f"📊 مجموع امتیاز: {total}\n"
        f"🎮 تعداد بازی: {games}"
    )


# =========================================================
# GAME TOP
# =========================================================

async def gametop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT
                u.first_name,
                u.username,
                g.best_score
            FROM game_results g
            LEFT JOIN users u
                ON u.user_id = g.user_id
            ORDER BY g.best_score DESC
            LIMIT 10
        """)

        rows = cur.fetchall()

        cur.close()

    finally:

        put_conn(conn)

    if not rows:

        await update.message.reply_text(
            "هنوز کسی بازی نکرده."
        )

        return

    text = "🏆 Subway Bird TOP\n\n"

    for i, row in enumerate(rows, 1):

        name = (
            row[0]
            or row[1]
            or "Unknown"
        )

        text += (
            f"{i}. {name} — "
            f"🏆 {row[2]}\n"
        )

    await update.message.reply_text(text)


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    logger.error(
        "Telegram error: %s",
        context.error
    )


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
# MAIN
# =========================================================

def main():

    init_db()

    # Flask
    Thread(
        target=run_flask,
        daemon=True
    ).start()

    application = (
        Application
        .builder()
        .token(TOKEN)
        .build()
    )

    #
application.add_handler(
    CommandHandler(
        "pay",
        pay
    )
) =====================================================
    # BASIC
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

    # =====================================================
    # ADMIN
    # =====================================================

    application.add_handler(
        CommandHandler(
            "addcoins",
            addcoins_command
        )
    )

    application.add_handler(
        CommandHandler(
            "removecoins",
            removecoins_command
        )
    )

    application.add_handler(
        CommandHandler(
            "addall",
            addall
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

    application.add_handler(
        PollAnswerHandler(
            poll_answer
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

    # =====================================================
    # GAME
    # =====================================================

    application.add_handler(
        CommandHandler(
            "gamestats",
            gamestats
        )
    )

    application.add_handler(
        CommandHandler(
            "gametop",
            gametop
        )
    )

    # =====================================================
    # TEXT
    # =====================================================

    application.add_handler(
        MessageHandler(
            filters.TEXT &
            ~filters.COMMAND,
            handle_message
        )
    )

    # =====================================================
    # ERRORS
    # =====================================================

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "Bot starting..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    main()
# =========================================================
# PAY / TRANSFER COINS
# =========================================================

async def pay(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    sender = update.effective_user

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "❌ روی پیام کاربری که می‌خوای براش کوین بفرستی Reply کن.\n\n"
            "مثال:\n"
            "/pay 100"
        )
        return

    if not context.args:
        await update.message.reply_text(
            "❌ مقدار رو وارد کن.\n\n"
            "مثال:\n"
            "/pay 100"
        )
        return

    try:
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ مقدار باید عدد باشه."
        )
        return

    if amount <= 0:
        await update.message.reply_text(
            "❌ مقدار باید بیشتر از صفر باشه."
        )
        return

    target = update.message.reply_to_message.from_user

    if target.id == sender.id:
        await update.message.reply_text(
            "😂 نمی‌تونی به خودت کوین بفرستی."
        )
        return

    ensure_user(sender)
    ensure_user(target)

    conn = get_conn()

    try:
        cur = conn.cursor()

        # قفل کردن موجودی فرستنده
        cur.execute("""
            SELECT coins
            FROM users
            WHERE user_id = %s
            FOR UPDATE
        """, (
            sender.id,
        ))

        sender_row = cur.fetchone()

        if not sender_row:
            conn.rollback()
            cur.close()

            await update.message.reply_text(
                "❌ حساب فرستنده پیدا نشد."
            )
            return

        sender_balance = sender_row[0]

        if sender_balance < amount:
            conn.rollback()
            cur.close()

            await update.message.reply_text(
                f"❌ موجودی کافی نیست.\n\n"
                f"💰 موجودی شما: {sender_balance}\n"
                f"🪙 مبلغ انتقال: {amount}"
            )
            return

        # کم کردن از فرستنده
        cur.execute("""
            UPDATE users
            SET coins = coins - %s
            WHERE user_id = %s
        """, (
            amount,
            sender.id
        ))

        # اضافه کردن به گیرنده
        cur.execute("""
            UPDATE users
            SET coins = coins + %s
            WHERE user_id = %s
        """, (
            amount,
            target.id
        ))

        conn.commit()
        cur.close()

    except Exception:
        conn.rollback()
        raise

    finally:
        put_conn(conn)

    await update.message.reply_text(
        f"✅ انتقال با موفقیت انجام شد!\n\n"
        f"👤 گیرنده: {target.first_name}\n"
        f"🪙 مبلغ: {amount} کوین\n"
        f"💰 موجودی جدید شما: {sender_balance - amount}"
    )
