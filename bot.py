import os
import re
import random
import json
import html
import time
import asyncio
import logging
from datetime import date, timedelta, datetime
from threading import Thread

import psycopg2
from psycopg2.extras import Json
from psycopg2.pool import ThreadedConnectionPool
from flask import Flask, request, jsonify

from telegram import (
    Update, Poll, InlineKeyboardButton, InlineKeyboardMarkup,
    WebAppInfo, ChatPermissions
)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ChatMemberHandler,
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

# The mini-game's own URL (hosted as a separate Render service).
# Can be overridden with a GAME_URL env var without touching code.
GAME_URL = os.environ.get("GAME_URL", "https://t-pk89.onrender.com")


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


@web.after_request
def add_cors_headers(response):
    # The mini-game HTML is loaded inside Telegram's in-app browser,
    # which treats it as a different origin than this Flask server.
    # Without these headers, the browser silently blocks the
    # game's fetch() to /game-score — no error shown to the player,
    # coins just never arrive. This allows it from anywhere.
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@web.route("/game-score", methods=["OPTIONS"])
def game_score_preflight():
    return ("", 204)


@web.route("/tap", methods=["OPTIONS"])
def tap_preflight():
    return ("", 204)


@web.route("/checkin", methods=["OPTIONS"])
def checkin_preflight():
    return ("", 204)


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


EXPECTED_SCHEMA = {
    "users": {
        "user_id": "BIGINT",
        "username": "TEXT",
        "first_name": "TEXT",
        "coins": "BIGINT DEFAULT 0",
        "total_coins": "BIGINT DEFAULT 0",
        "last_message": "DOUBLE PRECISION DEFAULT 0",
        "streak_count": "INTEGER DEFAULT 0",
        "last_checkin_date": "TEXT",
    },
    "bot_groups": {
        "chat_id": "BIGINT",
        "title": "TEXT",
    },
    "quiz_questions": {
        "id": None,  # serial, handled separately
        "question": "TEXT",
        "options": "TEXT[]",
        "correct_index": "INTEGER",
        "enabled": "BOOLEAN DEFAULT TRUE",
    },
    "quiz_polls": {
        "poll_id": "TEXT",
        "question_id": "INTEGER",
        "correct_index": "INTEGER",
    },
    "quiz_answers": {
        "poll_id": "TEXT",
        "user_id": "BIGINT",
        "answered_at": "DOUBLE PRECISION",
    },
    "quiz_cooldowns": {
        "chat_id": "BIGINT",
        "last_quiz": "DOUBLE PRECISION DEFAULT 0",
    },
    "quiz_user_stats": {
        "user_id": "BIGINT",
        "correct": "INTEGER DEFAULT 0",
        "wrong": "INTEGER DEFAULT 0",
        "total": "INTEGER DEFAULT 0",
    },
    "market": {
        "symbol": "TEXT",
        "name": "TEXT",
        "price": "BIGINT DEFAULT 100",
    },
    "market_holdings": {
        "user_id": "BIGINT",
        "symbol": "TEXT",
        "amount": "BIGINT DEFAULT 0",
    },
    "market_history": {
        "id": None,
        "symbol": "TEXT",
        "price": "BIGINT",
        "created_at": "DOUBLE PRECISION",
    },
    "market_groups": {
        "chat_id": "BIGINT",
    },
    "game_scores": {
        "id": None,
        "user_id": "BIGINT",
        "username": "TEXT",
        "name": "TEXT",
        "game_id": "TEXT",
        "score": "INTEGER DEFAULT 0",
        "coins_awarded": "INTEGER DEFAULT 0",
        "created_at": "DOUBLE PRECISION",
    },
    "game_results": {
        "user_id": "BIGINT",
        "best_score": "INTEGER DEFAULT 0",
        "total_score": "BIGINT DEFAULT 0",
        "games_played": "INTEGER DEFAULT 0",
    },
    "group_settings": {
        "chat_id": "BIGINT",
        "locks": "JSONB DEFAULT '{}'::jsonb",
        "welcome_enabled": "BOOLEAN DEFAULT FALSE",
        "welcome_text": "TEXT",
        "antiflood_enabled": "BOOLEAN DEFAULT FALSE",
        "antiflood_limit": "INTEGER DEFAULT 5",
        "antiflood_window": "INTEGER DEFAULT 10",
        "forcejoin_channel": "TEXT",
        "forceadd_required": "INTEGER DEFAULT 0",
    },
    "group_members": {
        "chat_id": "BIGINT",
        "user_id": "BIGINT",
        "username": "TEXT",
        "first_name": "TEXT",
    },
    "invite_counts": {
        "chat_id": "BIGINT",
        "user_id": "BIGINT",
        "count": "INTEGER DEFAULT 0",
    },
    "activity_stats": {
        "chat_id": "BIGINT",
        "user_id": "BIGINT",
        "message_count": "INTEGER DEFAULT 0",
    },
    "auto_replies": {
        "id": None,
        "chat_id": "BIGINT",
        "keyword": "TEXT",
        "response": "TEXT",
    },
    "filtered_words": {
        "id": None,
        "chat_id": "BIGINT",
        "word": "TEXT",
    },
    "warnings": {
        "chat_id": "BIGINT",
        "user_id": "BIGINT",
        "count": "INTEGER DEFAULT 0",
    },
}

# The column each table is keyed/looked-up by in ON CONFLICT clauses —
# must have a UNIQUE (or PRIMARY KEY) constraint for those to work.
UNIQUE_KEYS = {
    "users": "user_id",
    "bot_groups": "chat_id",
    "quiz_polls": "poll_id",
    "quiz_cooldowns": "chat_id",
    "quiz_user_stats": "user_id",
    "market": "symbol",
    "market_groups": "chat_id",
    "game_results": "user_id",
    "group_settings": "chat_id",
}


def _relax_unexpected_not_null_columns(conn, cur, table, expected_columns):
    """Safety net for any legacy column (from an older bot version)
    that isn't part of the current schema and still has a hard NOT
    NULL with no default — that would block every insert forever.
    Only drops the constraint; never touches data.
    """

    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = %s AND is_nullable = 'NO' AND column_default IS NULL
    """, (table,))

    for (col,) in cur.fetchall():

        if col in expected_columns:
            continue

        try:
            cur.execute(f'ALTER TABLE {table} ALTER COLUMN {col} DROP NOT NULL')
            conn.commit()
            logger.info(
                "Migration: relaxed NOT NULL on legacy column %s.%s", table, col
            )
        except Exception:
            conn.rollback()


def _add_composite_unique_if_missing(conn, cur, table, columns):
    """Same idea as _add_unique_if_missing but for a multi-column
    (composite) unique constraint, e.g. market_holdings(user_id, symbol),
    which is what ON CONFLICT (col_a, col_b) needs to be able to match.
    Just attempts to add it and quietly ignores failure if a matching
    constraint already exists (or if duplicate rows block it).
    """

    col_list = ", ".join(columns)
    constraint_name = f"{table}_{'_'.join(columns)}_key"

    try:
        cur.execute(f"""
            SELECT 1 FROM pg_constraint
            WHERE conname = %s AND conrelid = %s::regclass
        """, (constraint_name, table))

        if cur.fetchone() is not None:
            return  # we already added this one on a previous run

        cur.execute(
            f'ALTER TABLE {table} ADD CONSTRAINT {constraint_name} UNIQUE ({col_list})'
        )
        conn.commit()
        logger.info(
            "Migration: added composite unique constraint on %s(%s)",
            table, col_list
        )
    except Exception:
        conn.rollback()
        logger.warning(
            "Migration: could not add composite unique constraint on %s(%s) "
            "(it may already exist under a different name, or there may be "
            "duplicate rows already)",
            table, col_list
        )


def _migrate_existing_tables(conn, cur):

    for table, columns in EXPECTED_SCHEMA.items():

        for column, definition in columns.items():

            if definition is None:
                continue  # serial id columns — handled below

            _add_column_if_missing(cur, table, column, definition)

        conn.commit()

        _relax_unexpected_not_null_columns(conn, cur, table, set(columns.keys()))

        if "id" in columns:
            _ensure_serial_default(conn, cur, table, "id")

        if table in UNIQUE_KEYS:
            _add_unique_if_missing(conn, cur, table, UNIQUE_KEYS[table])

    # composite (multi-column) unique keys, for ON CONFLICT (a, b) clauses
    _add_composite_unique_if_missing(conn, cur, "market_holdings", ["user_id", "symbol"])
    _add_composite_unique_if_missing(conn, cur, "quiz_answers", ["poll_id", "user_id"])
    _add_composite_unique_if_missing(conn, cur, "warnings", ["chat_id", "user_id"])
    _add_composite_unique_if_missing(conn, cur, "group_members", ["chat_id", "user_id"])
    _add_composite_unique_if_missing(conn, cur, "invite_counts", ["chat_id", "user_id"])
    _add_composite_unique_if_missing(conn, cur, "activity_stats", ["chat_id", "user_id"])


def _ensure_default_market_row():
    """Guarantees the ANGRYCOIN market row exists. Safe to call any
    time (buy/sell call this if a lookup unexpectedly comes back
    empty) — never overwrites an existing price.
    """

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            INSERT INTO market (symbol, name, price)
            VALUES (%s, %s, %s)
            ON CONFLICT (symbol) DO NOTHING
        """, ("ANGRYCOIN", "AngryCoin", DEFAULT_ANGRYCOIN_PRICE))

        conn.commit()
        cur.close()

    except Exception:

        conn.rollback()
        logger.exception("Could not ensure default market row")

    finally:

        put_conn(conn)


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

        cur.execute("""
            CREATE TABLE IF NOT EXISTS group_settings (
                chat_id BIGINT PRIMARY KEY,
                locks JSONB DEFAULT '{}'::jsonb,
                welcome_enabled BOOLEAN DEFAULT FALSE,
                welcome_text TEXT,
                antiflood_enabled BOOLEAN DEFAULT FALSE,
                antiflood_limit INTEGER DEFAULT 5,
                antiflood_window INTEGER DEFAULT 10
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS filtered_words (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                word TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS warnings (
                chat_id BIGINT,
                user_id BIGINT,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS group_members (
                chat_id BIGINT,
                user_id BIGINT,
                username TEXT,
                first_name TEXT,
                PRIMARY KEY (chat_id, user_id)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS invite_counts (
                chat_id BIGINT,
                user_id BIGINT,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS activity_stats (
                chat_id BIGINT,
                user_id BIGINT,
                message_count INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS auto_replies (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                keyword TEXT,
                response TEXT
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
        "🎮 /play\n"
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
        "/play — شروع بازی\n"
        "/gamestats — آمار بازی\n"
        "/gametop — جدول رکوردها\n\n"
        "👑 دستورات ادمین:\n"
        "/addcoins 100 — با Reply یا @username\n"
        "/removecoins 100 — با Reply یا @username\n"
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
        "/updateprice — تغییر فوری قیمت (رندوم)\n"
        "/setmarketgroup — این گروه اعلان تغییر قیمت بگیره\n"
        "/unsetmarketgroup\n\n"
        "قیمت AngryCoin هر چند دقیقه خودکار (رندوم) عوض می‌شه "
        "و تو گروه‌های ثبت‌شده اعلام می‌شه.\n\n"
        "🛡 مدیریت گروه (فقط ادمین‌های گروه):\n"
        "/ban /unban /kick — با Reply\n"
        "/mute [دقیقه] /unmute — با Reply\n"
        "/warn /unwarn /warns — با Reply\n"
        "/lock نوع /unlock نوع /locks — مثال: /lock link\n"
        "  (link, forward, username, photo, video, sticker,\n"
        "   gif, voice, document, location, poll, contact)\n"
        "/filter کلمه /unfilter کلمه /filters\n"
        "/setwelcome متن — {name}=اسم کاربر\n"
        "/welcome on|off\n"
        "/antiflood on|off /setflood 5 10\n"
        "/promote /demote — با Reply\n"
        "/purge — با Reply، پاکسازی تا اینجا\n"
        "/tagall [متن] — تگ همه‌ی اعضای شناخته‌شده\n"
        "/setforcejoin @channel /unsetforcejoin\n"
        "/setforceadd عدد /myinvite\n"
        "/stats — فعال‌ترین اعضا\n"
        "/addreply کلمه | پاسخ /delreply کلمه /replies\n"
        "/mypanel — خلاصه‌ی وضعیت خودت\n"
        "/panel — منوی کامل با دکمه‌های شیشه‌ای\n\n"
        "🐦 برای گرفتن کوین هم بنویس:\n"
        "فولک\n"
        "یا\n"
        "هاپهاپ کوین\n\n"
        "📝 دستورات فارسی (بدون /):\n"
        "موجودی، برترین، بورس، پرتفوی، "
        "خرید 10، فروش 10، کوییز، راهنما، پنل\n\n"
        "📝 مدیریت گروه به فارسی (با Reply، فقط ادمین):\n"
        "بن، آنبن، اخراج، سکوت [دقیقه]، رفع سکوت، "
        "اخطار، حذف اخطار، اخطارها\n"
        "قفل [نوع]، بازکردن [نوع]، قفل‌ها\n"
        "فیلتر [کلمه]، حذف فیلتر [کلمه]، فیلترها\n"
        "خوشامد روشن/خاموش، تنظیم خوشامد [متن]\n"
        "ضدفلود روشن/خاموش، تنظیم فلود [عدد] [عدد]\n"
        "ارتقا، عزل، پاکسازی، تگ همه، همه\n"
        "تنظیم عضویت اجباری [@channel]، غیرفعال عضویت اجباری\n"
        "تنظیم اد اجباری [عدد]، دعوت من\n"
        "آمار، آمار فعالیت، پنل من\n"
        "تنظیم پاسخ [کلمه] | [پاسخ]، حذف پاسخ [کلمه]، پاسخ‌ها"
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

    text = (update.message.text or "").strip()

    # ---------------------------------------------------------
    # Persian word commands (so people can just type the word
    # instead of the English /command)
    # ---------------------------------------------------------

    NO_ARG_COMMANDS = {
        "موجودی": balance,
        "برترین": top,
        "جدول": top,
        "بورس": market,
        "بازار": market,
        "پرتفوی": portfolio,
        "راهنما": help_command,
        "کمک": help_command,
        "کوییز": quiz,
        "کویز": quiz,
        "سوال": quiz,
        "آمار بازی": gamestats,
        "رکورد": gametop,
        "بازی": play,
        "بازی کن": play,
        "بن": ban_command,
        "بن کن": ban_command,
        "آنبن": unban_command,
        "رفع بن": unban_command,
        "اخراج": kick_command,
        "رفع سکوت": unmute_command,
        "اخطار": warn_command,
        "حذف اخطار": unwarn_command,
        "اخطارها": warns_command,
        "اخطارهام": warns_command,
        "قفل ها": locks_command,
        "قفل‌ها": locks_command,
        "وضعیت قفل": locks_command,
        "فیلترها": filters_command,
        "لیست فیلتر": filters_command,
        "بروزرسانی قیمت": updateprice_command,
        "تغییر قیمت": updateprice_command,
        "پنل": panel_command,
        "ارتقا": promote_command,
        "عزل": demote_command,
        "پاکسازی": purge_command,
        "تگ همه": tagall_command,
        "همه": tagall_command,
        "دعوت من": myinvite_command,
        "لینک دعوت من": myinvite_command,
        "پنل من": mypanel_command,
        "آمار فعالیت": activitystats_command,
        "آمار": activitystats_command,
        "پاسخ ها": replies_command,
        "پاسخ‌ها": replies_command,
        "لیست پاسخ": replies_command,
        "غیرفعال عضویت اجباری": unsetforcejoin_command,
    }

    if text in NO_ARG_COMMANDS:
        await NO_ARG_COMMANDS[text](update, context)
        return

    buy_sell_match = re.match(r"^(خرید|فروش)\s+(\d+)$", text)

    if buy_sell_match:

        action_word, amount_str = buy_sell_match.groups()
        context.args = [amount_str]

        if action_word == "خرید":
            await buy(update, context)
        else:
            await sell(update, context)

        return

    # ---------------------------------------------------------
    # Persian group-moderation commands (admin-only — each of these
    # handlers checks is_group_admin itself and silently no-ops for
    # regular users, so it's safe to match on plain Persian words)
    # ---------------------------------------------------------

    mute_match = re.match(r"^سکوت(?:\s+(\d+))?$", text)
    if mute_match:
        minutes = mute_match.group(1)
        context.args = [minutes] if minutes else []
        await mute_command(update, context)
        return

    lock_match = re.match(r"^قفل\s+(.+)$", text)
    if lock_match:
        context.args = [lock_match.group(1)]
        await lock_command(update, context)
        return

    unlock_match = re.match(r"^(?:باز\s*کردن|بازکردن)\s+(.+)$", text)
    if unlock_match:
        context.args = [unlock_match.group(1)]
        await unlock_command(update, context)
        return

    filter_match = re.match(r"^فیلتر\s+(.+)$", text)
    if filter_match:
        context.args = [filter_match.group(1)]
        await filter_command(update, context)
        return

    unfilter_match = re.match(r"^حذف\s*فیلتر\s+(.+)$", text)
    if unfilter_match:
        context.args = [unfilter_match.group(1)]
        await unfilter_command(update, context)
        return

    welcome_toggle_match = re.match(r"^خوشامد\s+(روشن|خاموش)$", text)
    if welcome_toggle_match:
        context.args = [welcome_toggle_match.group(1)]
        await welcome_toggle_command(update, context)
        return

    setwelcome_match = re.match(r"^تنظیم\s*خوشامد\s+(.+)$", text)
    if setwelcome_match:
        context.args = setwelcome_match.group(1).split()
        await setwelcome_command(update, context)
        return

    antiflood_toggle_match = re.match(r"^ضدفلود\s+(روشن|خاموش)$", text)
    if antiflood_toggle_match:
        context.args = [antiflood_toggle_match.group(1)]
        await antiflood_toggle_command(update, context)
        return

    setflood_match = re.match(r"^تنظیم\s*فلود\s+(\d+)\s+(\d+)$", text)
    if setflood_match:
        context.args = [setflood_match.group(1), setflood_match.group(2)]
        await setflood_command(update, context)
        return

    forcejoin_match = re.match(r"^تنظیم\s*عضویت\s*اجباری\s+(.+)$", text)
    if forcejoin_match:
        context.args = [forcejoin_match.group(1)]
        await setforcejoin_command(update, context)
        return

    forceadd_match = re.match(r"^تنظیم\s*اد\s*اجباری\s+(\d+)$", text)
    if forceadd_match:
        context.args = [forceadd_match.group(1)]
        await setforceadd_command(update, context)
        return

    if re.match(r"^(پاسخ\s*خودکار\s*اضافه|تنظیم\s*پاسخ)\b", text):
        await addreply_command(update, context)
        return

    delreply_match = re.match(r"^حذف\s*پاسخ\s+(.+)$", text)
    if delreply_match:
        context.args = [delreply_match.group(1)]
        await delreply_command(update, context)
        return

    # ---------------------------------------------------------
    # Auto-reply keywords (admin-configured per group) — checked
    # for everyone, not just admins, since anyone can trigger one.
    # ---------------------------------------------------------

    if update.effective_chat.type in ("group", "supergroup"):

        auto_reply = await run_db(_match_auto_reply_db, update.effective_chat.id, text)

        if auto_reply:
            await update.message.reply_text(auto_reply)
            return

    if text not in ["فولک", "هاپهاپ کوین"]:
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
            SELECT user_id, first_name, username, coins
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
        user_id, first_name, username, coins = row
        name = html.escape(first_name or username or "Unknown")
        text += (
            f'{i}. <a href="tg://user?id={user_id}">{name}</a> '
            f'(ID: <code>{user_id}</code>) — 🪙 {coins}\n'
        )

    await update.message.reply_text(text, parse_mode="HTML")


# =========================================================
# ADMIN CHECK
# =========================================================

def is_admin(user_id):
    return user_id == ADMIN_ID


# =========================================================
# ADD / REMOVE COINS
# =========================================================

class _DBUser:
    """Minimal stand-in for a telegram.User, built from a DB lookup
    by username instead of from a Telegram Update object. Has the
    same .id/.username/.first_name attributes the rest of the code
    expects.
    """

    def __init__(self, id, username, first_name):
        self.id = id
        self.username = username
        self.first_name = first_name


def _find_user_by_username_db(username):

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT user_id, username, first_name
            FROM users
            WHERE LOWER(username) = LOWER(%s)
            LIMIT 1
        """, (username,))

        row = cur.fetchone()
        cur.close()

        if row:
            return _DBUser(row[0], row[1], row[2])

        return None

    finally:

        put_conn(conn)


async def _resolve_target_and_amount(update, context):
    """Shared by /addcoins and /removecoins: the target user can be
    given either by replying to their message, or by tagging them
    with @username in the command (e.g. /removecoins 100 @folan).
    Returns (target, amount), or (None, None) after already sending
    the person an explanation of what went wrong.
    """

    if update.message.reply_to_message:

        target = update.message.reply_to_message.from_user
        amount_arg = context.args[0] if context.args else None

    else:

        username_token = None
        amount_arg = None

        for token in context.args:

            if token.startswith("@"):
                username_token = token[1:]
            else:
                amount_arg = token

        if not username_token:
            await update.message.reply_text(
                "❌ یا روی پیام کاربر Reply کن، یا با @username تگش کن.\n\n"
                "مثال:\n/removecoins 100 @username"
            )
            return None, None

        target = await run_db(_find_user_by_username_db, username_token)

        if not target:
            await update.message.reply_text(
                "❌ کاربری با این username پیدا نشد "
                "(باید حداقل یه‌بار با ربات پیام داده باشه)."
            )
            return None, None

    if not amount_arg:
        await update.message.reply_text("❌ مقدار کوین رو وارد کن.")
        return None, None

    try:
        amount = int(amount_arg)
    except ValueError:
        await update.message.reply_text("❌ مقدار باید عدد باشه.")
        return None, None

    if amount <= 0:
        await update.message.reply_text("❌ مقدار باید بیشتر از صفر باشه.")
        return None, None

    return target, amount


async def addcoins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        return

    target, amount = await _resolve_target_and_amount(update, context)

    if target is None:
        return

    await run_db(ensure_user, target)
    await run_db(add_coins, target.id, amount)

    await update.message.reply_text(
        f"✅ {amount} کوین به {target.first_name or target.username} اضافه شد."
    )


async def removecoins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        return

    target, amount = await _resolve_target_and_amount(update, context)

    if target is None:
        return

    await run_db(ensure_user, target)
    await run_db(remove_coins, target.id, amount)

    await update.message.reply_text(
        f"✅ {amount} کوین از {target.first_name or target.username} کم شد."
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
            SELECT q.user_id, u.first_name, u.username, q.correct, q.total
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
        user_id, first_name, username, correct, total = row
        name = html.escape(first_name or username or "Unknown")
        text += (
            f'{i}. <a href="tg://user?id={user_id}">{name}</a> '
            f'(ID: <code>{user_id}</code>) — ✅ {correct} / {total}\n'
        )

    await update.message.reply_text(text, parse_mode="HTML")


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

    if not context.args:
        await update.message.reply_text(
            "/setprice 150\n"
            "یا برای چند سهمی:\n"
            "/setprice ANGRYCOIN 150"
        )
        return

    # One argument -> price for the default ANGRYCOIN market.
    # Two arguments -> symbol + price, for future multi-symbol use.
    if len(context.args) == 1:
        symbol = "ANGRYCOIN"
        price_arg = context.args[0]
    else:
        symbol = context.args[0].upper()
        price_arg = context.args[1]

    try:
        price = int(price_arg)
    except ValueError:
        await update.message.reply_text("❌ قیمت باید عدد باشد.")
        return

    if price < 0:
        await update.message.reply_text("❌ قیمت نمی‌تواند منفی باشد.")
        return

    await run_db(_ensure_default_market_row)

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

        cur.execute("SELECT price FROM market WHERE UPPER(TRIM(symbol)) = 'ANGRYCOIN'")
        row = cur.fetchone()

        if not row:

            # Self-heal: the default market row is missing for some
            # reason (fresh DB, wiped table, etc.) — create it and
            # continue instead of failing the purchase.
            cur.execute("""
                INSERT INTO market (symbol, name, price)
                VALUES (%s, %s, %s)
                ON CONFLICT (symbol) DO NOTHING
            """, ("ANGRYCOIN", "AngryCoin", DEFAULT_ANGRYCOIN_PRICE))

            conn.commit()

            cur.execute("SELECT price FROM market WHERE UPPER(TRIM(symbol)) = 'ANGRYCOIN'")
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

        cur.execute("SELECT price FROM market WHERE UPPER(TRIM(symbol)) = 'ANGRYCOIN'")
        price_row = cur.fetchone()

        if not price_row:

            cur.execute("""
                INSERT INTO market (symbol, name, price)
                VALUES (%s, %s, %s)
                ON CONFLICT (symbol) DO NOTHING
            """, ("ANGRYCOIN", "AngryCoin", DEFAULT_ANGRYCOIN_PRICE))

            conn.commit()

            cur.execute("SELECT price FROM market WHERE UPPER(TRIM(symbol)) = 'ANGRYCOIN'")
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
# AUTOMATIC RANDOM PRICING
# Every PRICE_UPDATE_INTERVAL_SECONDS, nudges the ANGRYCOIN price
# up or down by a random percentage and announces the change in
# every group registered via /setmarketgroup.
# =========================================================

PRICE_UPDATE_INTERVAL_SECONDS = int(
    os.environ.get("MARKET_UPDATE_MINUTES", "15")
) * 60

PRICE_CHANGE_MIN_PERCENT = -15
PRICE_CHANGE_MAX_PERCENT = 20


def _update_market_price_db():
    """Randomly nudges the ANGRYCOIN price. Returns (old_price,
    new_price), or None if the market row doesn't exist yet."""

    conn = get_conn()

    try:

        cur = conn.cursor()

        cur.execute("SELECT price FROM market WHERE UPPER(TRIM(symbol)) = 'ANGRYCOIN'")
        row = cur.fetchone()

        if not row:
            cur.close()
            return None

        old_price = row[0]

        pct = random.uniform(PRICE_CHANGE_MIN_PERCENT, PRICE_CHANGE_MAX_PERCENT)
        new_price = max(1, round(old_price * (1 + pct / 100)))

        cur.execute(
            "UPDATE market SET price = %s WHERE UPPER(TRIM(symbol)) = 'ANGRYCOIN'",
            (new_price,)
        )

        cur.execute("""
            INSERT INTO market_history (symbol, price, created_at)
            VALUES (%s, %s, %s)
        """, ("ANGRYCOIN", new_price, time.time()))

        conn.commit()
        cur.close()

        return old_price, new_price

    except Exception:

        conn.rollback()
        raise

    finally:

        put_conn(conn)


def _get_market_groups_db():

    conn = get_conn()

    try:

        cur = conn.cursor()
        cur.execute("SELECT chat_id FROM market_groups")
        rows = [r[0] for r in cur.fetchall()]
        cur.close()

        return rows

    finally:

        put_conn(conn)


def _format_price_change_message(old_price, new_price):

    diff = new_price - old_price
    pct = (diff / old_price * 100) if old_price else 0

    if diff > 0:
        arrow = "📈"
    elif diff < 0:
        arrow = "📉"
    else:
        arrow = "➖"

    sign = "+" if diff >= 0 else ""

    return (
        f"{arrow} قیمت AngryCoin تغییر کرد!\n\n"
        f"قیمت قبلی: {old_price}\n"
        f"قیمت جدید: {new_price}\n"
        f"تغییر: {sign}{diff} ({sign}{pct:.1f}٪)"
    )


async def _broadcast_price_change(application, old_price, new_price):

    text = _format_price_change_message(old_price, new_price)
    groups = await run_db(_get_market_groups_db)

    for chat_id in groups:
        try:
            await application.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.warning("Could not send price update to %s: %s", chat_id, e)


async def market_price_updater_loop(application):
    """Runs forever in the background (started from post_init, so it
    shares the same event loop run_polling uses — no extra thread,
    no APScheduler/JobQueue dependency needed)."""

    while True:

        await asyncio.sleep(PRICE_UPDATE_INTERVAL_SECONDS)

        try:

            result = await run_db(_update_market_price_db)

            if not result:
                continue

            old_price, new_price = result

            await _broadcast_price_change(application, old_price, new_price)

        except Exception:

            logger.exception("Market price updater error")


async def updateprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: trigger an immediate random price change, instead
    of waiting for the next scheduled interval — handy for testing."""

    if not is_admin(update.effective_user.id):
        return

    result = await run_db(_update_market_price_db)

    if not result:
        await update.message.reply_text("❌ بازار موجود نیست.")
        return

    old_price, new_price = result

    await update.message.reply_text(_format_price_change_message(old_price, new_price))

    await _broadcast_price_change(context.application, old_price, new_price)


# =========================================================
# ADMIN PANEL (inline "glass" buttons — like DigiAnti's menu)
# =========================================================
# Each entry: (callback_data, button_label, implemented?)
# "implemented" entries show real command help; the rest show an
# honest "coming soon" instead of pretending the feature exists.

PANEL_CATEGORIES = [
    ("panel_locks",     "🔒 مدیریت قفل‌ها",        True),
    ("panel_users",     "👮 مجازات کاربران",        True),
    ("panel_promote",   "⬆️ ارتقا و عزل کاربران",   True),
    ("panel_filter",    "🚫 فیلتر کلمات",           True),
    ("panel_purge",     "🧹 پاکسازی",               True),
    ("panel_welcome",   "👋 خوش‌آمدگویی",           True),
    ("panel_forcejoin", "📢 عضویت اجباری",          True),
    ("panel_forceadd",  "➕ اد اجباری",             True),
    ("panel_flood",     "🌊 ضدفلود",                True),
    ("panel_stats",     "📊 آمار فعالیت‌ها",        True),
    ("panel_autoreply", "🤖 پاسخ خودکار",           True),
    ("panel_market",    "📈 بازار AngryCoin",       True),
    ("panel_fun",       "🎮 سرگرمی و کاربردی",      True),
    ("panel_userpanel", "👤 پنل کاربر",             True),
    ("panel_settings",  "⚙️ تنظیمات عمومی",         True),
    ("panel_tagall",    "📣 تگ کردن همه",           True),
]

PANEL_CONTENT = {
    "panel_locks": (
        "🔒 مدیریت قفل‌ها\n\n"
        "/lock نوع — قفل کردن\n"
        "/unlock نوع — باز کردن\n"
        "/locks — وضعیت فعلی\n\n"
        "انواع: link, forward, username, photo, video, sticker, "
        "gif, voice, document, location, poll, contact\n\n"
        "یا فارسی: «قفل لینک»، «بازکردن لینک»، «قفل‌ها»"
    ),
    "panel_users": (
        "👮 مجازات کاربران\n\n"
        "/ban /unban /kick — با Reply\n"
        "/mute [دقیقه] /unmute — با Reply\n"
        "/warn /unwarn /warns — با Reply\n\n"
        "یا فارسی (با Reply): «بن»، «اخراج»، «سکوت 30»، "
        "«رفع سکوت»، «اخطار»، «حذف اخطار»، «اخطارها»"
    ),
    "panel_filter": (
        "🚫 فیلتر کلمات\n\n"
        "/filter کلمه — اضافه کردن\n"
        "/unfilter کلمه — حذف\n"
        "/filters — لیست\n\n"
        "یا فارسی: «فیلتر کلمه»، «حذف فیلتر کلمه»، «فیلترها»"
    ),
    "panel_welcome": (
        "👋 خوش‌آمدگویی\n\n"
        "/setwelcome متن — {name} جای اسم کاربر میاد\n"
        "/welcome on|off\n\n"
        "یا فارسی: «تنظیم خوشامد متن...»، «خوشامد روشن/خاموش»"
    ),
    "panel_flood": (
        "🌊 ضدفلود (پیام رگباری)\n\n"
        "/antiflood on|off\n"
        "/setflood تعداد ثانیه — مثال: /setflood 5 10\n"
        "(بیشتر از ۵ پیام تو ۱۰ ثانیه = ۱ دقیقه سکوت خودکار)\n\n"
        "یا فارسی: «ضدفلود روشن/خاموش»، «تنظیم فلود 5 10»"
    ),
    "panel_market": (
        "📈 بازار AngryCoin\n\n"
        "/market /buy /sell /portfolio\n"
        "/setprice /updateprice\n"
        "/setmarketgroup /unsetmarketgroup\n\n"
        "قیمت هر چند دقیقه خودکار و رندوم تغییر می‌کنه و "
        "تو گروه‌های ثبت‌شده اعلام می‌شه."
    ),
    "panel_fun": (
        "🎮 سرگرمی و کاربردی\n\n"
        "/quiz — سوال کوییز\n"
        "/play — بازی AngryCoin (Web App)\n"
        "برای گرفتن کوین هم بنویس «فولک»"
    ),
    "panel_promote": (
        "⬆️ ارتقا و عزل کاربران\n\n"
        "/promote — با Reply، ادمین می‌کنه\n"
        "/demote — با Reply، عزل می‌کنه\n\n"
        "یا فارسی (با Reply): «ارتقا»، «عزل»"
    ),
    "panel_purge": (
        "🧹 پاکسازی\n\n"
        "روی پیامی که می‌خوای پاکسازی از اونجا شروع بشه Reply کن و بنویس:\n"
        "/purge  یا  «پاکسازی»\n\n"
        "همه‌ی پیام‌های بین اون پیام تا پیام فعلی پاک می‌شن "
        "(حداکثر ۳۰۰ پیام یکجا)."
    ),
    "panel_forcejoin": (
        "📢 عضویت اجباری\n\n"
        "/setforcejoin @channelusername — فعال کردن\n"
        "/unsetforcejoin — غیرفعال کردن\n\n"
        "یا فارسی: «تنظیم عضویت اجباری @channel»، «غیرفعال عضویت اجباری»\n\n"
        "⚠️ ربات باید عضو همون کانال باشه تا بتونه عضویت رو چک کنه."
    ),
    "panel_forceadd": (
        "➕ اد اجباری\n\n"
        "/setforceadd عدد — مثلاً /setforceadd 3 (۰ = غیرفعال)\n"
        "/myinvite — گرفتن لینک دعوت شخصی\n\n"
        "یا فارسی: «تنظیم اد اجباری 3»، «دعوت من»\n\n"
        "کاربرایی که به حد نصاب نرسیده باشن نمی‌تونن پیام بدن."
    ),
    "panel_stats": (
        "📊 آمار فعالیت‌ها\n\n"
        "/stats — ۱۰ نفر فعال‌تر این گروه بر اساس تعداد پیام\n\n"
        "یا فارسی: «آمار» یا «آمار فعالیت»"
    ),
    "panel_autoreply": (
        "🤖 پاسخ خودکار\n\n"
        "/addreply کلمه | پاسخ — اضافه کردن\n"
        "/delreply کلمه — حذف\n"
        "/replies — لیست کلمات\n\n"
        "یا فارسی: «تنظیم پاسخ کلمه | پاسخ»، «حذف پاسخ کلمه»، «پاسخ‌ها»"
    ),
    "panel_userpanel": (
        "👤 پنل کاربر\n\n"
        "/mypanel — خلاصه‌ی وضعیت خودت (کوین، اخطار، دعوت، پیام)\n\n"
        "یا فارسی: «پنل من»"
    ),
    "panel_tagall": (
        "📣 تگ کردن همه\n\n"
        "/tagall [متن دلخواه] — تگ همه‌ی اعضایی که تا الان تو گروه "
        "پیام دادن (فقط همینا شناخته شده‌ن)\n\n"
        "یا فارسی: «تگ همه» یا «همه»"
    ),
    "panel_settings": (
        "⚙️ تنظیمات عمومی — فهرست کامل دستورات مدیریتی\n\n"
        "برای دیدن جزئیات هر بخش، از همین پنل روی دکمه‌ی مربوطه بزن، "
        "یا /help رو تو PV بزن."
    ),
}

COMING_SOON_LABELS = {
    data: label
    for data, label, implemented in PANEL_CATEGORIES
    if not implemented
}


def build_panel_keyboard():

    rows = []
    row = []

    for data, label, implemented in PANEL_CATEGORIES:
        row.append(InlineKeyboardButton(label, callback_data=data))
        if len(row) == 2:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    return InlineKeyboardMarkup(rows)


PANEL_INTRO_TEXT = "📋 راهنمای ربات\n\nیه بخش رو انتخاب کن:"


async def panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    chat = update.effective_chat
    user = update.effective_user

    allowed = False

    if chat.type == "private" and is_admin(user.id):
        allowed = True
    elif chat.type in ("group", "supergroup") and await is_group_admin(update, context):
        allowed = True

    if not allowed:
        return

    await update.message.reply_text(
        PANEL_INTRO_TEXT,
        reply_markup=build_panel_keyboard()
    )


async def panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    data = query.data
    back_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 بازگشت", callback_data="panel_back")]]
    )

    if data == "panel_back":
        await query.edit_message_text(PANEL_INTRO_TEXT, reply_markup=build_panel_keyboard())
        return

    if data in COMING_SOON_LABELS:
        label = COMING_SOON_LABELS[data]
        text = f"{label}\n\n🚧 این بخش هنوز اضافه نشده — به‌زودی."
    else:
        text = PANEL_CONTENT.get(data, "❌ یافت نشد.")

    await query.edit_message_text(text, reply_markup=back_keyboard)


# =========================================================
# GAME SCORE API (Flask — runs in its own thread already,
# so it does NOT need run_db; it's fine to be blocking here)
# =========================================================

STREAK_BONUS_PER_DAY = 5
TAP_ANTI_ABUSE_CAP = 1000  # max taps accepted in a single /tap request


@web.route("/balance", methods=["GET"])
def balance_api():
    """Lets a mini-game show the player's current wallet balance
    without needing them to type /balance in the chat.
    Usage: GET /balance?user_id=12345
    """

    try:

        user_id = request.args.get("user_id")

        if not user_id:
            return jsonify({"ok": False, "error": "missing_user_id"}), 400

        try:
            user_id = int(user_id)
        except ValueError:
            return jsonify({"ok": False, "error": "invalid_user_id"}), 400

        conn = get_conn()

        try:

            cur = conn.cursor()

            cur.execute(
                "SELECT coins, streak_count FROM users WHERE user_id = %s",
                (user_id,)
            )

            row = cur.fetchone()
            cur.close()

        finally:

            put_conn(conn)

        coins = row[0] if row else 0
        streak = (row[1] if row and row[1] else 0)

        return jsonify({"ok": True, "coins": coins, "streak": streak})

    except Exception:

        logger.exception("Balance API error")
        return jsonify({"ok": False, "error": "server_error"}), 500


@web.route("/checkin", methods=["POST"])
def checkin():
    """Daily streak check-in (TikTok-style). Call this once when a
    mini-game opens. Awards a small coin bonus the first time each
    day, and tracks a consecutive-day streak that resets if a day
    is missed.
    """

    try:

        data = request.get_json(silent=True)

        if not data:
            return jsonify({"ok": False, "error": "invalid_json"}), 400

        user_id = data.get("user_id")
        name = str(data.get("name") or "Player")[:100]

        if user_id is None:
            return jsonify({"ok": False, "error": "missing_user_id"}), 400

        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid_user_id"}), 400

        today = date.today()
        today_str = today.isoformat()

        conn = get_conn()

        try:

            cur = conn.cursor()

            cur.execute("""
                INSERT INTO users (user_id, first_name)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET first_name = EXCLUDED.first_name
            """, (user_id, name))

            cur.execute("""
                SELECT streak_count, last_checkin_date
                FROM users WHERE user_id = %s
            """, (user_id,))

            row = cur.fetchone()
            streak, last_date_str = row
            streak = streak or 0

            already_checked_in_today = (last_date_str == today_str)
            bonus = 0

            if not already_checked_in_today:

                if last_date_str:

                    last_checkin = date.fromisoformat(last_date_str)

                    if today - last_checkin == timedelta(days=1):
                        streak += 1
                    else:
                        streak = 1  # streak broken — missed a day

                else:

                    streak = 1  # first ever check-in

                bonus = STREAK_BONUS_PER_DAY * min(streak, 7)

                cur.execute("""
                    UPDATE users
                    SET
                        streak_count = %s,
                        last_checkin_date = %s,
                        coins = coins + %s,
                        total_coins = total_coins + %s
                    WHERE user_id = %s
                """, (streak, today_str, bonus, bonus, user_id))

            conn.commit()
            cur.close()

        except Exception:

            conn.rollback()
            raise

        finally:

            put_conn(conn)

        return jsonify({
            "ok": True,
            "streak": streak,
            "already_checked_in_today": already_checked_in_today,
            "bonus_awarded": bonus
        })

    except Exception:

        logger.exception("Checkin error")
        return jsonify({"ok": False, "error": "server_error"}), 500


@web.route("/tap", methods=["POST"])
def tap_earn():
    """Called by the AngryCoin Tap mini-game. Adds 1 coin per tap
    (batched — the game sends taps in small groups, not one request
    per tap) directly to the player's wallet.
    """

    try:

        data = request.get_json(silent=True)

        if not data:
            return jsonify({"ok": False, "error": "invalid_json"}), 400

        user_id = data.get("user_id")
        name = str(data.get("name") or "Player")[:100]
        taps = data.get("taps", 0)

        if user_id is None:
            return jsonify({"ok": False, "error": "missing_user_id"}), 400

        try:
            user_id = int(user_id)
            taps = int(taps)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid_values"}), 400

        if user_id <= 0:
            return jsonify({"ok": False, "error": "invalid_user_id"}), 400

        if taps <= 0:
            return jsonify({"ok": False, "error": "invalid_taps"}), 400

        if taps > TAP_ANTI_ABUSE_CAP:
            taps = TAP_ANTI_ABUSE_CAP

        conn = get_conn()

        try:

            cur = conn.cursor()

            cur.execute("""
                INSERT INTO users (user_id, first_name, coins, total_coins)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    first_name = EXCLUDED.first_name,
                    coins = users.coins + EXCLUDED.coins,
                    total_coins = users.total_coins + EXCLUDED.total_coins
            """, (user_id, name, taps, taps))

            cur.execute(
                "SELECT coins FROM users WHERE user_id = %s",
                (user_id,)
            )

            new_balance = cur.fetchone()[0]

            conn.commit()
            cur.close()

        except Exception:

            conn.rollback()
            raise

        finally:

            put_conn(conn)

        return jsonify({
            "ok": True,
            "taps_added": taps,
            "coins": new_balance
        })

    except Exception:

        logger.exception("Tap earn error")
        return jsonify({"ok": False, "error": "server_error"}), 500


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


async def play(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 بازی کن (Subway Bird)", web_app=WebAppInfo(url=GAME_URL))]
    ])

    await update.message.reply_text(
        "برای بازی و گرفتن کوین، روی دکمه زیر بزن 👇\n\n"
        "⚠️ حتماً از همین دکمه باز کن (نه لینک مستقیم)، "
        "وگرنه کوینی که می‌گیری به حسابت اضافه نمی‌شه.",
        reply_markup=keyboard
    )


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
            SELECT g.user_id, u.first_name, u.username, g.best_score
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
        user_id, first_name, username, best_score = row
        name = html.escape(first_name or username or "Unknown")
        text += (
            f'{i}. <a href="tg://user?id={user_id}">{name}</a> '
            f'(ID: <code>{user_id}</code>) — 🏆 {best_score}\n'
        )

    await update.message.reply_text(text, parse_mode="HTML")


# =========================================================
# =========================================================
# GROUP MODERATION (locks, mute/ban/warn, filters, welcome,
# antiflood) — everything a "guardian" group bot needs
# =========================================================

LOCK_NAMES = [
    "link", "forward", "username", "photo", "video",
    "sticker", "gif", "voice", "document", "location",
    "poll", "contact",
]

LOCK_LABELS_FA = {
    "link": "لینک",
    "forward": "فوروارد",
    "username": "منشن",
    "photo": "عکس",
    "video": "فیلم",
    "sticker": "استیکر",
    "gif": "گیف",
    "voice": "صدا",
    "document": "فایل",
    "location": "