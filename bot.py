import os
import time
import asyncio
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
# DATABASE POOL
# =========================================================
# CHANGE: get_conn() now verifies the connection is actually
# alive (SELECT 1) before handing it out, and transparently
# reconnects if the pooled connection was dropped by the DB
# provider (common on free-tier Postgres after idle timeout,
# or right after Render wakes the service back up).

db_pool = ThreadedConnectionPool(
    minconn=1,
    maxconn=15,
    dsn=DATABASE_URL,
    connect_timeout=10,
    sslmode="require"
)


def get_conn():

    last_error = None

    for _ in range(3):

        conn = db_pool.getconn()

        try:

            if conn.closed:
                db_pool.putconn(conn, close=True)
                continue

            with conn.cursor() as test_cur:
                test_cur.execute("SELECT 1")

            return conn

        except Exception as e:

            last_error = e

            try:
                db_pool.putconn(conn, close=True)
            except Exception:
                pass

    logger.warning(
        "Pool connections looked dead (%s), opening a direct connection.",
        last_error
    )

    return psycopg2.connect(
        DATABASE_URL,
        connect_timeout=10,
        sslmode="require"
    )


def put_conn(conn):

    try:
        db_pool.putconn(conn)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


# =========================================================
# ASYNC/SYNC BRIDGE
# =========================================================
# CHANGE: every function below that talks to Postgres is a
# normal *blocking* function. Handlers must never call them
# directly with `await` missing — they must go through
# run_db(), which offloads the blocking call to a worker
# thread via asyncio.to_thread. This stops one slow/stuck DB
# call from freezing the whole bot (all chats, all users)
# until it finishes.

async def run_db(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


# =========================================================
# SELF-HEALING SCHEMA MIGRATION
# =========================================================
# CHANGE: if a table already existed in the database (e.g. from
# an earlier version of the bot) with a different/older shape,
# CREATE TABLE IF NOT EXISTS silently does nothing and the old
# shape stays broken. These helpers patch an *existing* table in
# place — adding missing columns or constraints — WITHOUT ever
# dropping or clearing data. Existing rows (old user coin
# balances, etc.) are always preserved.

def _add_column_if_missing(cur, table, column, definition):

    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
    """, (table, column))

    if cur.fetchone() is None:
        cur.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        logger.info("Migration: added column %s.%s", table, column)


def _add_unique_if_missing(conn, cur, table, column):

    cur.execute("""
        SELECT 1
        FROM pg_index i
        JOIN pg_attribute a
            ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = %s::regclass
          AND i.indisunique
          AND a.attname = %s
    """, (table, column))

    if cur.fetchone() is None:

        try:
            cur.execute(
                f'ALTER TABLE {table} ADD CONSTRAINT {table}_{column}_key UNIQUE ({column})'
            )
            conn.commit()
            logger.info("Migration: added unique constraint on %s.%s", table, column)
        except Exception:
            conn.rollback()
            logger.warning(
                "Migration: could not add unique constraint on %s.%s "
                "(there may be duplicate values already in that column)",
                table, column
            )


def _ensure_serial_default(conn, cur, table, column):
    """If `column` exists but has no DEFAULT (so inserts that don't
    mention it fail with NotNullViolation), give it an auto-increment
    default backed by a sequence, continuing from the current max
    value. Never touches existing rows.
    """

    cur.execute("""
        SELECT column_default FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
    """, (table, column))

    row = cur.fetchone()

    if row is None:
        return  # column doesn't exist on this table, nothing to do

    if row[0] is not None:
        return  # already has a default

    seq_name = f"{table}_{column}_seq"

    try:
        cur.execute(f'CREATE SEQUENCE IF NOT EXISTS {seq_name}')
        cur.execute(
            f"SELECT setval('{seq_name}', COALESCE((SELECT MAX({column}) FROM {table}), 0) + 1, false)"
        )
        cur.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT nextval('{seq_name}')"
        )
        conn.commit()
        logger.info("Migration: added auto-increment default to %s.%s", table, column)
    except Exception:
        conn.rollback()
        logger.warning(
            "Migration: could not add auto-increment default to %s.%s",
            table, column
        )


def _drop_not_null_if_exists(conn, cur, table, column):
    """If a legacy column still has a NOT NULL constraint but the
    current bot code never writes to it, drop just that constraint
    so old rows/new inserts stop failing. The column and its data
    (if any) are left in place — nothing is deleted.
    """

    cur.execute("""
        SELECT is_nullable FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
    """, (table, column))

    row = cur.fetchone()

    if row is None:
        return  # column doesn't exist on this table

    if row[0] == "YES":
        return  # already nullable

    try:
        cur.execute(f'ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL')
        conn.commit()
        logger.info("Migration: dropped NOT NULL on %s.%s (legacy column)", table, column)
    except Exception:
        conn.rollback()
        logger.warning(
            "Migration: could not drop NOT NULL on %s.%s", table, column
        )


def _migrate_existing_tables(conn, cur):

    # users — this is the table that was missing columns in your logs
    _add_column_if_missing(cur, "users", "username", "TEXT")
    _add_column_if_missing(cur, "users", "first_name", "TEXT")
    _add_column_if_missing(cur, "users", "coins", "BIGINT DEFAULT 0")
    _add_column_if_missing(cur, "users", "total_coins", "BIGINT DEFAULT 0")
    _add_column_if_missing(cur, "users", "last_message", "DOUBLE PRECISION DEFAULT 0")
    conn.commit()

    # legacy columns from an older schema version that the current
    # bot code doesn't write to — make sure they can't block inserts
    _drop_not_null_if_exists(conn, cur, "users", "name")

    # market — this is the table that was missing its UNIQUE/PK constraint
    _add_column_if_missing(cur, "market", "name", "TEXT")
    _add_column_if_missing(cur, "market", "price", "BIGINT DEFAULT 100")
    conn.commit()
    _add_unique_if_missing(conn, cur, "market", "symbol")
    _ensure_serial_default(conn, cur, "market", "id")


# =========================================================
# DB INIT
# =========================================================

def init_db():

    conn = get_conn()

    try:

        cur = conn.cursor()

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

        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_groups (
                chat_id BIGINT PRIMARY KEY,
                title TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS quiz_questions (
                id SERIAL PRIMARY KEY,
                question TEXT NOT NULL,
                options TEXT[] NOT NULL,
                correct_index INTEGER NOT NULL,
                enabled BOOLEAN DEFAULT TRUE
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS quiz_polls (
                poll_id TEXT PRIMARY KEY,
                question_id INTEGER,
                correct_index INTEGER
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS quiz_answers (
                poll_id TEXT,
                user_id BIGINT,
                answered_at DOUBLE PRECISION,
                PRIMARY KEY (poll_id, user_id)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS quiz_cooldowns (
                chat_id BIGINT PRIMARY KEY,
                last_quiz DOUBLE PRECISION DEFAULT 0
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS quiz_user_stats (
                user_id BIGINT PRIMARY KEY,
                correct INTEGER DEFAULT 0,
                wrong INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS market (
                symbol TEXT PRIMARY KEY,
                name TEXT,
                price BIGINT DEFAULT 100
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_holdings (
                user_id BIGINT,
                symbol TEXT,
                amount BIGINT DEFAULT 0,
                PRIMARY KEY (user_id, symbol)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_history (
                id SERIAL PRIMARY KEY,
                symbol TEXT,
                price BIGINT,
                created_at DOUBLE PRECISION
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_groups (
                chat_id BIGINT PRIMARY KEY
            )
        """)

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

        cur.execute("""
            CREATE TABLE IF NOT EXISTS game_results (
                user_id BIGINT PRIMARY KEY,
                best_score INTEGER DEFAULT 0,
                total_score BIGINT DEFAULT 0,
                games_played INTEGER DEFAULT 0
            )
        """)

        conn.commit()

        # Patch up any pre-existing tables that were created by an
        # older version of the bot with a different shape.
        _migrate_existing_tables(conn, cur)

        cur.execute("""
            INSERT INTO market (symbol, name, price)
            VALUES (%s, %s, %s)
            ON CONFLICT (symbol) DO NOTHING
        """, (
            "ANGRYCOIN",
            "AngryCoin",
            DEFAULT_ANGRYCOIN_PRICE,
        ))

        conn.commit()
        cur.close()

        logger.info("Database initialized.")

    except Exception:

        conn.rollback()
        logger.exception("Database initialization error")

    finally:

        put_conn(conn)


# =========================================================
# USER FUNCTIONS (blocking — always call via run_db)
# =========================================================

def ensure_user(user):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            INSERT INTO users (user_id, username, first_name)
            VALUES (%s, %s, %s)
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

    except Exception:

        conn.rollback()
        raise

    finally:

        put_conn(conn)


def get_balance(user_id):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute(
            "SELECT coins FROM users WHERE user_id = %s",
            (user_id,)
        )

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
            INSERT INTO users (user_id, coins, total_coins)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id)
            DO UPDATE SET
                coins = users.coins + EXCLUDED.coins,
                total_coins = users.total_coins + EXCLUDED.total_coins
        """, (
            user_id,
            amount,
            amount
        ))

        conn.commit()
        cur.close()

    except Exception:

        conn.rollback()
        raise

    finally:

        put_conn(conn)


def remove_coins(user_id, amount):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            UPDATE users
            SET coins = GREATEST(coins - %s, 0)
            WHERE user_id = %s
        """, (
            amount,
            user_id
        ))

        conn.commit()
        cur.close()

    except Exception:

        conn.rollback()
        raise

    finally:

        put_conn(conn)


# =========================================================
# START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if user:
        await run_db(ensure_user, user)

    await update.message.reply_text(
        "🐦 سلام!\n\n"
        "🪙 ربات کوین فعاله.\n\n"
        "💰 /balance\n"
        "🏆 /top\n"
        "💸 /pay 100 (با Reply)\n"
        "🧠 /quiz\n"
        "📈 /market\n"
        "🎮 /gamestats\n"
        "❓ /help"
    )


# =========================================================
# HELP
# =========================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = (
        "📚 راهنمای ربات\n"
        "━━━━━━━━━━━━━━\n\n"
        "💰 بخش کوین:\n"
        "/balance — موجودی\n"
        "/top — جدول برترین‌ها\n"
        "/pay 100 — انتقال کوین با Reply\n\n"
        "🧠 بخش Quiz:\n"
        "/quiz — سوال جدید\n"
        "/quizscore — امتیاز شما\n"
        "/quiztop — جدول Quiz\n\n"
        "📈 بخش AngryCoin:\n"
        "/market — قیمت بازار\n"
        "/buy 10 — خرید سهم\n"
        "/sell 10 — فروش سهم\n"
        "/portfolio — پرتفوی\n\n"
        "🎮 بخش Subway Bird:\n"
        "/gamestats — آمار بازی\n"
        "/gametop — جدول رکوردها\n\n"
        "👑 دستورات ادمین:\n"
        "/addcoins 100 — با Reply\n"
        "/removecoins 100 — با Reply\n"
        "/addall 100 — به همه\n"
        "/playerstats — آمار کاربر\n"
        "/say متن\n"
        "/groupmsg متن\n"
        "/setgroup\n\n"
        "🧠 مدیریت Quiz:\n"
        "/addquestion\n"
        "/questions\n"
        "/delquestion ID\n"
        "/enablequestion ID\n"
        "/disablequestion ID\n\n"
        "📈 مدیریت بازار:\n"
        "/setprice ANGRYCOIN 150\n"
        "/setmarketgroup\n"
        "/unsetmarketgroup\n\n"
        "🐦 برای گرفتن کوین هم بنویس:\n"
        "فولک\n"
        "یا\n"
        "جیک"
    )

    await update.message.reply_text(text)


# =========================================================
# MESSAGE COINS
# =========================================================

def _handle_message_db(user_id, now):
    """Blocking helper for the message-coin cooldown logic."""

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute(
            "SELECT last_message FROM users WHERE user_id = %s",
            (user_id,)
        )

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
                user_id
            ))

            conn.commit()
            cur.close()

            return ("awarded", None)

        else:

            cur.close()
            remaining = int(COOLDOWN - (now - last))
            return ("cooldown", remaining)

    except Exception:

        conn.rollback()
        logger.exception("Message coin error")
        return ("error", None)

    finally:

        put_conn(conn)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return

    user = update.effective_user

    if not user:
        return

    await run_db(ensure_user, user)

    text = update.message.text or ""

    if text.strip() not in ["فولک", "جیک کوین"]:
        return

    now = time.time()

    status, remaining = await run_db(_handle_message_db, user.id, now)

    if status == "awarded":

        await update.message.reply_text(
            f"🪙 +{COINS_PER_MESSAGE} کوین گرفتی!"
        )

    elif status == "cooldown":

        minutes = remaining // 60
        seconds = remaining % 60

        await update.message.reply_text(
            f"⏳ هنوز زوده!\n"
            f"{minutes} دقیقه و {seconds} ثانیه دیگه امتحان کن."
        )

    # status == "error": stay silent, already logged


# =========================================================
# BALANCE
# =========================================================

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    await run_db(ensure_user, user)
    coins = await run_db(get_balance, user.id)

    await update.message.reply_text(
        f"💰 موجودی شما:\n\n🪙 {coins} کوین"
    )


# =========================================================
# TOP
# =========================================================

def _top_db():

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT first_name, username, coins
            FROM users
            ORDER BY coins DESC
            LIMIT 10
        """)

        rows = cur.fetchall()
        cur.close()

        return rows

    finally:

        put_conn(conn)


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):

    rows = await run_db(_top_db)

    if not rows:
        await update.message.reply_text("هنوز کسی کوین نداره 😅")
        return

    text = "🏆 TOP 10\n\n"

    for i, row in enumerate(rows, 1):
        name = row[0] or row[1] or "Unknown"
        coins = row[2]
        text += f"{i}. {name} — 🪙 {coins}\n"

    await update.message.reply_text(text)


# =========================================================
# ADMIN CHECK
# =========================================================

def is_admin(user_id):
    return user_id == ADMIN_ID


# =========================================================
# ADD / REMOVE COINS
# =========================================================

async def addcoins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "❌ روی پیام کاربر Reply کن:\n/addcoins 100"
        )
        return

    if not context.args:
        await update.message.reply_text("❌ مقدار کوین رو وارد کن.")
        return

    try:
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ مقدار باید عدد باشه.")
        return

    if amount <= 0:
        await update.message.reply_text("❌ مقدار باید بیشتر از صفر باشه.")
        return

    target = update.message.reply_to_message.from_user

    await run_db(ensure_user, target)
    await run_db(add_coins, target.id, amount)

    await update.message.reply_text(
        f"✅ {amount} کوین به {target.first_name} اضافه شد."
    )


async def removecoins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("❌ روی پیام کاربر Reply کن.")
        return

    if not context.args:
        await update.message.reply_text("❌ مقدار کوین رو وارد کن.")
        return

    try:
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ مقدار باید عدد باشه.")
        return

    if amount <= 0:
        await update.message.reply_text("❌ مقدار باید بیشتر از صفر باشه.")
        return

    target = update.message.reply_to_message.from_user

    await run_db(ensure_user, target)
    await run_db(remove_coins, target.id, amount)

    await update.message.reply_text(
        f"✅ {amount} کوین از {target.first_name} کم شد."
    )


# =========================================================
# PAY / TRANSFER
# =========================================================

def _pay_db(sender, target, amount):
    """Returns (ok: bool, message: str)."""

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            INSERT INTO users (user_id, username, first_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id)
            DO UPDATE SET username = EXCLUDED.username, first_name = EXCLUDED.first_name
        """, (sender.id, sender.username, sender.first_name))

        cur.execute("""
            INSERT INTO users (user_id, username, first_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id)
            DO UPDATE SET username = EXCLUDED.username, first_name = EXCLUDED.first_name
        """, (target.id, target.username, target.first_name))

        cur.execute(
            "SELECT coins FROM users WHERE user_id = %s FOR UPDATE",
            (sender.id,)
        )

        sender_row = cur.fetchone()

        if not sender_row:
            conn.rollback()
            return False, "❌ حساب فرستنده پیدا نشد."

        sender_balance = sender_row[0]

        if sender_balance < amount:
            conn.rollback()
            return False, (
                f"❌ موجودی کافی نیست.\n\n"
                f"💰 موجودی شما: {sender_balance}\n"
                f"🪙 مبلغ انتقال: {amount}"
            )

        cur.execute(
            "SELECT user_id FROM users WHERE user_id = %s FOR UPDATE",
            (target.id,)
        )

        target_row = cur.fetchone()

        if not target_row:
            conn.rollback()
            return False, "❌ حساب گیرنده پیدا نشد."

        cur.execute(
            "UPDATE users SET coins = coins - %s WHERE user_id = %s",
            (amount, sender.id)
        )

        cur.execute(
            "UPDATE users SET coins = coins + %s WHERE user_id = %s",
            (amount, target.id)
        )

        conn.commit()
        cur.close()

        new_balance = sender_balance - amount

        return True, (
            f"✅ انتقال با موفقیت انجام شد!\n\n"
            f"👤 گیرنده: {target.first_name}\n"
            f"🪙 مبلغ: {amount} کوین\n"
            f"💰 موجودی جدید شما: {new_balance}"
        )

    except Exception:

        conn.rollback()
        logger.exception("Pay transfer error")
        return False, "❌ انتقال انجام نشد. دوباره امتحان کن."

    finally:

        put_conn(conn)


async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):

    sender = update.effective_user

    if not sender:
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "❌ روی پیام کسی که می‌خوای براش کوین بفرستی Reply کن.\n\n"
            "مثال:\n/pay 100"
        )
        return

    if not context.args:
        await update.message.reply_text(
            "❌ مقدار کوین رو وارد کن.\n\nمثال:\n/pay 100"
        )
        return

    try:
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ مقدار باید عدد باشه.")
        return

    if amount <= 0:
        await update.message.reply_text("❌ مقدار باید بیشتر از صفر باشه.")
        return

    target = update.message.reply_to_message.from_user

    if not target:
        await update.message.reply_text("❌ گیرنده پیدا نشد.")
        return

    if target.id == sender.id:
        await update.message.reply_text("😂 نمی‌تونی به خودت کوین بفرستی.")
        return

    ok, message = await run_db(_pay_db, sender, target, amount)

    await update.message.reply_text(message)


# =========================================================
# ADD ALL
# =========================================================

def _addall_db(amount):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            UPDATE users
            SET coins = coins + %s, total_coins = total_coins + %s
        """, (amount, amount))

        count = cur.rowcount
        conn.commit()
        cur.close()

        return count

    except Exception:

        conn.rollback()
        raise

    finally:

        put_conn(conn)


async def addall(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("مثال:\n/addall 100")
        return

    try:
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ مقدار باید عدد باشد.")
        return

    if amount <= 0:
        await update.message.reply_text("❌ مقدار باید بیشتر از صفر باشد.")
        return

    count = await run_db(_addall_db, amount)

    await update.message.reply_text(
        f"✅ به {count} کاربر، نفری {amount} کوین اضافه شد."
    )


# =========================================================
# PLAYER STATS
# =========================================================

def _playerstats_db(user_id):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT user_id, first_name, username, coins, total_coins
            FROM users
            WHERE user_id = %s
        """, (user_id,))

        row = cur.fetchone()

        cur.execute("""
            SELECT best_score, total_score, games_played
            FROM game_results
            WHERE user_id = %s
        """, (user_id,))

        game_row = cur.fetchone()

        cur.close()

        return row, game_row

    finally:

        put_conn(conn)


async def playerstats(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        return

    target_user = None

    if update.message.reply_to_message:

        target_user = update.message.reply_to_message.from_user
        await run_db(ensure_user, target_user)
        target_id = target_user.id

    elif context.args:

        try:
            target_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ ID باید عدد باشد.")
            return

    else:

        await update.message.reply_text("❌ روی پیام کاربر Reply کن یا ID بده.")
        return

    row, game_row = await run_db(_playerstats_db, target_id)

    if not row:
        await update.message.reply_text("❌ کاربر پیدا نشد.")
        return

    user_id, first_name, username, coins, total_coins = row

    if game_row:
        best_score, total_score, games_played = game_row
    else:
        best_score, total_score, games_played = 0, 0, 0

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

async def say(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        return

    await update.message.reply_text(" ".join(context.args))


# =========================================================
# SET GROUP
# =========================================================

def _setgroup_db(chat_id, title):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            INSERT INTO bot_groups (chat_id, title)
            VALUES (%s, %s)
            ON CONFLICT (chat_id) DO UPDATE SET title = EXCLUDED.title
        """, (chat_id, title))

        conn.commit()
        cur.close()

    except Exception:

        conn.rollback()
        raise

    finally:

        put_conn(conn)


async def setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        return

    chat = update.effective_chat

    await run_db(_setgroup_db, chat.id, chat.title or "")

    await update.message.reply_text("✅ این گروه به عنوان گروه ربات ثبت شد.")


# =========================================================
# GROUP MESSAGE
# =========================================================

def _get_groups_db():

    conn = get_conn()

    try:

        cur = conn.cursor()
        cur.execute("SELECT chat_id FROM bot_groups")
        rows = cur.fetchall()
        cur.close()

        return rows

    finally:

        put_conn(conn)


async def groupmsg(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("متن پیام رو بنویس.")
        return

    message = " ".join(context.args)

    groups = await run_db(_get_groups_db)

    sent = 0

    for row in groups:

        try:
            await context.bot.send_message(chat_id=row[0], text=message)
            sent += 1
        except Exception as e:
            logger.warning("Could not send group message: %s", e)

    await update.message.reply_text(f"📢 پیام به {sent} گروه ارسال شد.")


# =========================================================
# QUIZ
# =========================================================

def _quiz_pick_question_db(chat_id, now):
    """Checks cooldown and, if allowed, picks a question and marks cooldown.
    Returns one of:
      ("cooldown", remaining_seconds)
      ("no_question", None)
      ("ok", (question_id, text, options, correct_index))
    """

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute(
            "SELECT last_quiz FROM quiz_cooldowns WHERE chat_id = %s",
            (chat_id,)
        )

        row = cur.fetchone()
        last = row[0] if row else 0

        if now - last < QUIZ_COOLDOWN:
            remaining = int(QUIZ_COOLDOWN - (now - last))
            cur.close()
            return "cooldown", remaining

        cur.execute("""
            SELECT id, question, options, correct_index
            FROM quiz_questions
            WHERE enabled = TRUE
            ORDER BY RANDOM()
            LIMIT 1
        """)

        question = cur.fetchone()

        if not question:
            cur.close()
            return "no_question", None

        cur.execute("""
            INSERT INTO quiz_cooldowns (chat_id, last_quiz)
            VALUES (%s, %s)
            ON CONFLICT (chat_id) DO UPDATE SET last_quiz = EXCLUDED.last_quiz
        """, (chat_id, now))

        conn.commit()
        cur.close()

        return "ok", question

    finally:

        put_conn(conn)


def _quiz_clear_cooldown_db(chat_id, now):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            DELETE FROM quiz_cooldowns
            WHERE chat_id = %s AND last_quiz = %s
        """, (chat_id, now))

        conn.commit()
        cur.close()

    finally:

        put_conn(conn)


def _quiz_save_poll_db(poll_id, question_id, correct_index):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            INSERT INTO quiz_polls (poll_id, question_id, correct_index)
            VALUES (%s, %s, %s)
            ON CONFLICT (poll_id) DO NOTHING
        """, (poll_id, question_id, correct_index))

        conn.commit()
        cur.close()

    finally:

        put_conn(conn)


async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):

    chat_id = update.effective_chat.id
    now = time.time()

    status, payload = await run_db(_quiz_pick_question_db, chat_id, now)

    if status == "cooldown":

        remaining = payload
        minutes = remaining // 60
        seconds = remaining % 60

        await update.message.reply_text(
            f"⏳ سوال بعدی تا {minutes}:{seconds:02d}"
        )

        return

    if status == "no_question":
        await update.message.reply_text("❌ هنوز سوالی ثبت نشده.")
        return

    question_id, text, options, correct_index = payload

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

        await run_db(_quiz_clear_cooldown_db, chat_id, now)
        raise

    poll_id = message.poll.id

    await run_db(_quiz_save_poll_db, poll_id, question_id, correct_index)


# =========================================================
# POLL ANSWER
# =========================================================

def _poll_answer_db(poll_id, user, selected):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute(
            "SELECT correct_index FROM quiz_polls WHERE poll_id = %s",
            (poll_id,)
        )

        row = cur.fetchone()

        if not row:
            cur.close()
            return

        correct_index = row[0]

        cur.execute("""
            INSERT INTO quiz_answers (poll_id, user_id, answered_at)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (poll_id, user.id, time.time()))

        if cur.rowcount == 0:
            conn.commit()
            cur.close()
            return

        if selected == correct_index:

            cur.execute("""
                INSERT INTO users (user_id, username, first_name, coins, total_coins)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id)
                DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    coins = users.coins + EXCLUDED.coins,
                    total_coins = users.total_coins + EXCLUDED.total_coins
            """, (user.id, user.username, user.first_name, QUIZ_REWARD, QUIZ_REWARD))

            cur.execute("""
                INSERT INTO quiz_user_stats (user_id, correct, wrong, total)
                VALUES (%s, 1, 0, 1)
                ON CONFLICT (user_id)
                DO UPDATE SET
                    correct = quiz_user_stats.correct + 1,
                    total = quiz_user_stats.total + 1
            """, (user.id,))

        else:

            cur.execute("""
                INSERT INTO quiz_user_stats (user_id, correct, wrong, total)
                VALUES (%s, 0, 1, 1)
                ON CONFLICT (user_id)
                DO UPDATE SET
                    wrong = quiz_user_stats.wrong + 1,
                    total = quiz_user_stats.total + 1
            """, (user.id,))

        conn.commit()
        cur.close()

    except Exception:

        conn.rollback()
        logger.exception("Poll answer error")

    finally:

        put_conn(conn)


async def poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):

    answer = update.poll_answer

    poll_id = answer.poll_id
    user = answer.user

    if not answer.option_ids:
        return

    selected = answer.option_ids[0]

    await run_db(_poll_answer_db, poll_id, user, selected)


# =========================================================
# QUIZ SCORE
# =========================================================

def _quizscore_db(user_id):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT correct, wrong, total
            FROM quiz_user_stats
            WHERE user_id = %s
        """, (user_id,))

        row = cur.fetchone()
        cur.close()

        return row

    finally:

        put_conn(conn)


async def quizscore(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    row = await run_db(_quizscore_db, user.id)

    if not row:
        await update.message.reply_text("هنوز در هیچ کوییزی شرکت نکردی.")
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

def _quiztop_db():

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT u.first_name, u.username, q.correct, q.total
            FROM quiz_user_stats q
            LEFT JOIN users u ON u.user_id = q.user_id
            ORDER BY q.correct DESC
            LIMIT 10
        """)

        rows = cur.fetchall()
        cur.close()

        return rows

    finally:

        put_conn(conn)


async def quiztop(update: Update, context: ContextTypes.DEFAULT_TYPE):

    rows = await run_db(_quiztop_db)

    if not rows:
        await update.message.reply_text("هنوز کسی در کوئیز شرکت نکرده.")
        return

    text = "🏆 Quiz TOP\n\n"

    for i, row in enumerate(rows, 1):
        name = row[0] or row[1] or "Unknown"
        text += f"{i}. {name} — ✅ {row[2]} / {row[3]}\n"

    await update.message.reply_text(text)


# =========================================================
# ADD QUESTION
# =========================================================

def _addquestion_db(question, options, correct_index):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            INSERT INTO quiz_questions (question, options, correct_index, enabled)
            VALUES (%s, %s, %s, TRUE)
            RETURNING id
        """, (question, options, correct_index))

        question_id = cur.fetchone()[0]

        conn.commit()
        cur.close()

        return question_id

    except Exception:

        conn.rollback()
        raise

    finally:

        put_conn(conn)


async def addquestion(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        return

    raw = update.message.text or ""

    if raw.startswith("/addquestion"):
        raw = raw[len("/addquestion"):].strip()

    if not raw:
        await update.message.reply_text(
            "فرمت:\n\n"
            "/addquestion سوال | گزینه1 | گزینه2 | گزینه3 | گزینه4 | شماره جواب درست\n\n"
            "مثال:\n"
            "/addquestion پایتخت ایران چیست؟ | تهران | شیراز | تبریز | اهواز | 1"
        )
        return

    parts = [x.strip() for x in raw.split("|")]

    if len(parts) != 6:
        await update.message.reply_text(
            "❌ فرمت اشتباهه.\n\n"
            "باید دقیقاً این شکلی باشه:\n"
            "/addquestion سوال | گزینه1 | گزینه2 | گزینه3 | گزینه4 | شماره جواب درست"
        )
        return

    question = parts[0]
    options = parts[1:5]

    try:
        correct_index = int(parts[5]) - 1
    except ValueError:
        await update.message.reply_text("❌ شماره جواب درست باید 1 تا 4 باشد.")
        return

    if not question:
        await update.message.reply_text("❌ متن سوال خالیه.")
        return

    if any(not option for option in options):
        await update.message.reply_text("❌ گزینه‌ها نباید خالی باشند.")
        return

    if correct_index not in range(4):
        await update.message.reply_text("❌ شماره جواب درست باید بین 1 تا 4 باشد.")
        return

    try:
        question_id = await run_db(_addquestion_db, question, options, correct_index)
    except Exception:
        logger.exception("Add question error")
        await update.message.reply_text("❌ خطا در ذخیره سوال.")
        return

    await update.message.reply_text(f"✅ سوال با ID {question_id} اضافه شد.")


# =========================================================
# QUESTIONS LIST / DELETE / ENABLE / DISABLE
# =========================================================

def _questions_db():

    conn = get_conn()

    try:

        cur = conn.cursor()
        cur.execute("SELECT id, question, enabled FROM quiz_questions ORDER BY id")
        rows = cur.fetchall()
        cur.close()

        return rows

    finally:

        put_conn(conn)


async def questions(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        return

    rows = await run_db(_questions_db)

    if not rows:
        await update.message.reply_text("هیچ سوالی وجود نداره.")
        return

    text = "📚 Questions\n\n"

    for row in rows:
        status = "🟢" if row[2] else "🔴"
        text += f"{row[0]}. {status} {row[1]}\n"

    await update.message.reply_text(text)


def _delquestion_db(qid):

    conn = get_conn()

    try:

        cur = conn.cursor()
        cur.execute("DELETE FROM quiz_questions WHERE id = %s", (qid,))
        deleted = cur.rowcount
        conn.commit()
        cur.close()

        return deleted

    finally:

        put_conn(conn)


async def delquestion(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("/delquestion 1")
        return

    try:
        qid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID باید عدد باشد.")
        return

    deleted = await run_db(_delquestion_db, qid)

    if deleted:
        await update.message.reply_text(f"🗑 سوال {qid} حذف شد.")
    else:
        await update.message.reply_text("❌ سوال پیدا نشد.")


def _set_question_enabled_db(qid, enabled):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute(
            "UPDATE quiz_questions SET enabled = %s WHERE id = %s",
            (enabled, qid)
        )

        changed = cur.rowcount
        conn.commit()
        cur.close()

        return changed

    finally:

        put_conn(conn)


async def enablequestion(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("/enablequestion 1")
        return

    try:
        qid = int(context.args[0])
    except ValueError:
        return

    changed = await run_db(_set_question_enabled_db, qid, True)

    await update.message.reply_text(
        f"🟢 سوال {qid} فعال شد." if changed else "❌ سوال پیدا نشد."
    )


async def disablequestion(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("/disablequestion 1")
        return

    try:
        qid = int(context.args[0])
    except ValueError:
        return

    changed = await run_db(_set_question_enabled_db, qid, False)

    await update.message.reply_text(
        f"🔴 سوال {qid} غیرفعال شد." if changed else "❌ سوال پیدا نشد."
    )


# =========================================================
# MARKET
# =========================================================

def _market_db():

    conn = get_conn()

    try:

        cur = conn.cursor()
        cur.execute("SELECT symbol, name, price FROM market ORDER BY symbol")
        rows = cur.fetchall()
        cur.close()

        return rows

    finally:

        put_conn(conn)


async def market(update: Update, context: ContextTypes.DEFAULT_TYPE):

    rows = await run_db(_market_db)

    text = "📈 بازار AngryCoin\n\n"

    for symbol, name, price in rows:
        text += f"🪙 {name}\nSymbol: {symbol}\nPrice: {price}\n\n"

    await update.message.reply_text(text)


def _setprice_db(symbol, price):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute(
            "UPDATE market SET price = %s WHERE symbol = %s",
            (price, symbol)
        )

        if cur.rowcount == 0:
            conn.rollback()
            cur.close()
            return False

        cur.execute("""
            INSERT INTO market_history (symbol, price, created_at)
            VALUES (%s, %s, %s)
        """, (symbol, price, time.time()))

        conn.commit()
        cur.close()

        return True

    finally:

        put_conn(conn)


async def setprice(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        return

    if len(context.args) < 2:
        await update.message.reply_text("/setprice ANGRYCOIN 150")
        return

    symbol = context.args[0].upper()

    try:
        price = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ قیمت باید عدد باشد.")
        return

    if price < 0:
        await update.message.reply_text("❌ قیمت نمی‌تواند منفی باشد.")
        return

    ok = await run_db(_setprice_db, symbol, price)

    if not ok:
        await update.message.reply_text(f"❌ سهم {symbol} وجود ندارد.")
        return

    await update.message.reply_text(f"✅ قیمت {symbol} شد {price}")


# =========================================================
# BUY / SELL
# =========================================================

def _buy_db(user_id, amount):
    """Returns (ok, message)."""

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("SELECT price FROM market WHERE symbol = 'ANGRYCOIN'")
        row = cur.fetchone()

        if not row:
            cur.close()
            return False, "❌ بازار موجود نیست."

        price = row[0]
        total_cost = amount * price

        cur.execute("SELECT coins FROM users WHERE user_id = %s", (user_id,))
        balance_row = cur.fetchone()
        balance_value = balance_row[0] if balance_row else 0

        if balance_value < total_cost:
            cur.close()
            return False, (
                f"❌ کوین کافی نداری.\n"
                f"هزینه: {total_cost}\n"
                f"موجودی: {balance_value}"
            )

        cur.execute(
            "UPDATE users SET coins = coins - %s WHERE user_id = %s",
            (total_cost, user_id)
        )

        cur.execute("""
            INSERT INTO market_holdings (user_id, symbol, amount)
            VALUES (%s, 'ANGRYCOIN', %s)
            ON CONFLICT (user_id, symbol)
            DO UPDATE SET amount = market_holdings.amount + EXCLUDED.amount
        """, (user_id, amount))

        conn.commit()
        cur.close()

        return True, (
            f"✅ خرید انجام شد!\n\n🪙 مقدار: {amount}\n💰 هزینه: {total_cost}"
        )

    except Exception:

        conn.rollback()
        raise

    finally:

        put_conn(conn)


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user
    await run_db(ensure_user, user)

    if not context.args:
        await update.message.reply_text("/buy 10")
        return

    try:
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ مقدار باید عدد باشد.")
        return

    if amount <= 0:
        await update.message.reply_text("❌ مقدار باید بیشتر از صفر باشد.")
        return

    ok, message = await run_db(_buy_db, user.id, amount)

    await update.message.reply_text(message)


def _sell_db(user_id, amount):
    """Returns (ok, message)."""

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("SELECT price FROM market WHERE symbol = 'ANGRYCOIN'")
        price_row = cur.fetchone()

        if not price_row:
            cur.close()
            return False, "❌ بازار موجود نیست."

        price = price_row[0]

        cur.execute("""
            SELECT amount FROM market_holdings
            WHERE user_id = %s AND symbol = 'ANGRYCOIN'
        """, (user_id,))

        row = cur.fetchone()
        owned = row[0] if row else 0

        if owned < amount:
            cur.close()
            return False, f"❌ این مقدار رو نداری.\nموجودی سهم: {owned}"

        value = amount * price

        cur.execute("""
            UPDATE market_holdings
            SET amount = amount - %s
            WHERE user_id = %s AND symbol = 'ANGRYCOIN'
        """, (amount, user_id))

        cur.execute(
            "UPDATE users SET coins = coins + %s WHERE user_id = %s",
            (value, user_id)
        )

        conn.commit()
        cur.close()

        return True, (
            f"✅ فروش انجام شد!\n\n🪙 مقدار: {amount}\n💰 دریافتی: {value}"
        )

    except Exception:

        conn.rollback()
        raise

    finally:

        put_conn(conn)


async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user
    await run_db(ensure_user, user)

    if not context.args:
        await update.message.reply_text("/sell 10")
        return

    try:
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ مقدار باید عدد باشد.")
        return

    if amount <= 0:
        await update.message.reply_text("❌ مقدار باید بیشتر از صفر باشد.")
        return

    ok, message = await run_db(_sell_db, user.id, amount)

    await update.message.reply_text(message)


# =========================================================
# PORTFOLIO
# =========================================================

def _portfolio_db(user_id):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT h.symbol, h.amount, m.price
            FROM market_holdings h
            JOIN market m ON m.symbol = h.symbol
            WHERE h.user_id = %s AND h.amount > 0
        """, (user_id,))

        rows = cur.fetchall()
        cur.close()

        return rows

    finally:

        put_conn(conn)


async def portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user
    await run_db(ensure_user, user)

    rows = await run_db(_portfolio_db, user.id)

    if not rows:
        await update.message.reply_text("📊 پرتفوی شما خالیه.")
        return

    text = "📊 Portfolio\n\n"
    total = 0

    for symbol, amount, price in rows:
        value = amount * price
        total += value
        text += f"🪙 {symbol}\nتعداد: {amount}\nقیمت: {price}\nارزش: {value}\n\n"

    text += f"💰 ارزش کل: {total}"

    await update.message.reply_text(text)


# =========================================================
# MARKET GROUP
# =========================================================

def _setmarketgroup_db(chat_id):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute(
            "INSERT INTO market_groups (chat_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (chat_id,)
        )

        conn.commit()
        cur.close()

    finally:

        put_conn(conn)


async def setmarketgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        return

    await run_db(_setmarketgroup_db, update.effective_chat.id)

    await update.message.reply_text("📈 این گروه برای بازار ثبت شد.")


def _unsetmarketgroup_db(chat_id):

    conn = get_conn()

    try:

        cur = conn.cursor()
        cur.execute("DELETE FROM market_groups WHERE chat_id = %s", (chat_id,))
        conn.commit()
        cur.close()

    finally:

        put_conn(conn)


async def unsetmarketgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        return

    await run_db(_unsetmarketgroup_db, update.effective_chat.id)

    await update.message.reply_text("✅ گروه از بازار حذف شد.")


# =========================================================
# GAME SCORE API (Flask — runs in its own thread already,
# so it does NOT need run_db; it's fine to be blocking here)
# =========================================================

@web.route("/game-score", methods=["POST"])
def game_score():

    try:

        data = request.get_json(silent=True)

        if not data:
            return jsonify({"ok": False, "error": "invalid_json"}), 400

        user_id = data.get("user_id")
        name = str(data.get("name") or "Player")[:100]
        game_id = str(data.get("game_id") or "subway_bird")[:50]
        score = data.get("score", 0)

        if user_id is None:
            return jsonify({"ok": False, "error": "missing_user_id"}), 400

        try:
            user_id = int(user_id)
            score = int(score)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid_values"}), 400

        if user_id <= 0:
            return jsonify({"ok": False, "error": "invalid_user_id"}), 400

        if score < 0:
            score = 0

        if score > 1000000:
            return jsonify({"ok": False, "error": "score_too_high"}), 400

        coins_awarded = score * GAME_COIN_MULTIPLIER

        conn = get_conn()

        try:

            cur = conn.cursor()

            cur.execute("""
                SELECT best_score, total_score, games_played
                FROM game_results
                WHERE user_id = %s
            """, (user_id,))

            old = cur.fetchone()

            if old:
                old_best, old_total, old_games = old
                new_best = max(old_best, score)
                new_total = old_total + score
                new_games = old_games + 1
            else:
                new_best = score
                new_total = score
                new_games = 1

            cur.execute("""
                INSERT INTO game_results (user_id, best_score, total_score, games_played)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id)
                DO UPDATE SET
                    best_score = EXCLUDED.best_score,
                    total_score = EXCLUDED.total_score,
                    games_played = EXCLUDED.games_played
            """, (user_id, new_best, new_total, new_games))

            cur.execute("""
                INSERT INTO game_scores (
                    user_id, username, name, game_id, score, coins_awarded, created_at
                )
                VALUES (%s, NULL, %s, %s, %s, %s, %s)
            """, (user_id, name, game_id, score, coins_awarded, time.time()))

            cur.execute("""
                INSERT INTO users (user_id, first_name, coins, total_coins)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id)
                DO UPDATE SET
                    first_name = EXCLUDED.first_name,
                    coins = users.coins + EXCLUDED.coins,
                    total_coins = users.total_coins + EXCLUDED.total_coins
            """, (user_id, name, coins_awarded, coins_awarded))

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

        logger.exception("Game score error")
        return jsonify({"ok": False, "error": "server_error"}), 500


# =========================================================
# GAME STATS
# =========================================================

def _gamestats_db(user_id):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT best_score, total_score, games_played
            FROM game_results
            WHERE user_id = %s
        """, (user_id,))

        row = cur.fetchone()
        cur.close()

        return row

    finally:

        put_conn(conn)


async def gamestats(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    row = await run_db(_gamestats_db, user.id)

    if not row:
        await update.message.reply_text("🎮 هنوز بازی نکردی!")
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

def _gametop_db():

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT u.first_name, u.username, g.best_score
            FROM game_results g
            LEFT JOIN users u ON u.user_id = g.user_id
            ORDER BY g.best_score DESC
            LIMIT 10
        """)

        rows = cur.fetchall()
        cur.close()

        return rows

    finally:

        put_conn(conn)


async def gametop(update: Update, context: ContextTypes.DEFAULT_TYPE):

    rows = await run_db(_gametop_db)

    if not rows:
        await update.message.reply_text("هنوز کسی بازی نکرده.")
        return

    text = "🏆 Subway Bird TOP\n\n"

    for i, row in enumerate(rows, 1):
        name = row[0] or row[1] or "Unknown"
        text += f"{i}. {name} — 🏆 {row[2]}\n"

    await update.message.reply_text(text)


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Telegram error: %s", context.error)


# =========================================================
# FLASK SERVER
# =========================================================

def run_flask():
    # CHANGE: threaded=True so a slow DB call from the game-score
    # endpoint doesn't block /health pings (which is what an uptime
    # monitor needs to keep Render's free instance awake).
    web.run(
        host="0.0.0.0",
        port=PORT,
        use_reloader=False,
        threaded=True
    )


# =========================================================
# MAIN
# =========================================================

def main():

    init_db()

    Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(TOKEN).build()

    # BASIC
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("top", top))
    application.add_handler(CommandHandler("pay", pay))

    # ADMIN
    application.add_handler(CommandHandler("addcoins", addcoins_command))
    application.add_handler(CommandHandler("removecoins", removecoins_command))
    application.add_handler(CommandHandler("addall", addall))
    application.add_handler(CommandHandler("playerstats", playerstats))
    application.add_handler(CommandHandler("say", say))
    application.add_handler(CommandHandler("groupmsg", groupmsg))
    application.add_handler(CommandHandler("setgroup", setgroup))

    # QUIZ
    application.add_handler(CommandHandler("quiz", quiz))
    application.add_handler(CommandHandler("quizscore", quizscore))
    application.add_handler(CommandHandler("quiztop", quiztop))
    application.add_handler(CommandHandler("addquestion", addquestion))
    application.add_handler(CommandHandler("questions", questions))
    application.add_handler(CommandHandler("delquestion", delquestion))
    application.add_handler(CommandHandler("enablequestion", enablequestion))
    application.add_handler(CommandHandler("disablequestion", disablequestion))
    application.add_handler(PollAnswerHandler(poll_answer))

    # MARKET
    application.add_handler(CommandHandler("market", market))
    application.add_handler(CommandHandler("buy", buy))
    application.add_handler(CommandHandler("sell", sell))
    application.add_handler(CommandHandler("portfolio", portfolio))
    application.add_handler(CommandHandler("setmarketgroup", setmarketgroup))
    application.add_handler(CommandHandler("unsetmarketgroup", unsetmarketgroup))
    application.add_handler(CommandHandler("setprice", setprice))

    # GAME
    application.add_handler(CommandHandler("gamestats", gamestats))
    application.add_handler(CommandHandler("gametop", gametop))

    # TEXT
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    # ERRORS
    application.add_error_handler(error_handler)

    logger.info("Bot starting...")

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
