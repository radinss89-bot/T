import os
import time
import json
from threading import Thread

import psycopg2
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


# =========================
# SETTINGS
# =========================

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = 6235380364

COINS_PER_MESSAGE = 10
COOLDOWN = 2 * 60

GAME_URL = "https://t-pk89.onrender.com/"
COINS_PER_GAME_SCORE = 5


# =========================
# DATABASE
# =========================

def get_db():
    return psycopg2.connect(
        os.environ["DATABASE_URL"]
    )


def init_db():
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
    cur.close()
    conn.close()


# =========================
# USER
# =========================

def get_user(user_id, name):
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
        """, (user_id, name))

        conn.commit()
        coins = 0
        last = 0

    else:
        coins, last = row

    cur.close()
    conn.close()

    return coins, last


def update_user(user_id, name, coins, last):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO users
        (user_id, name, coins, last_message)
        VALUES (%s, %s, %s, %s)

        ON CONFLICT (user_id)
        DO UPDATE SET
            name = EXCLUDED.name,
            coins = EXCLUDED.coins,
            last_message = EXCLUDED.last_message
    """, (user_id, name, coins, last))

    conn.commit()
    cur.close()
    conn.close()


# =========================
# فولک
# =========================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = update.message

    if not message or not message.text or not message.from_user:
        return

    if message.text.strip() != "فولک":
        return

    user = message.from_user
    user_id = user.id
    name = user.first_name or "کاربر"

    now = time.time()

    coins, last = get_user(user_id, name)

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


# =========================
# BALANCE
# =========================

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_user or not update.message:
        return

    user = update.effective_user
    name = user.first_name or "کاربر"

    coins, _ = get_user(
        user.id,
        name
    )

    await update.message.reply_text(
        f"💰 موجودی شما: {coins} کوین"
    )


# =========================
# TOP
# =========================

async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT name, coins
        FROM users
        ORDER BY coins DESC
        LIMIT 10
    """)

    ranking = cur.fetchall()

    cur.close()
    conn.close()

    if not ranking:
        await update.message.reply_text(
            "🏆 هنوز کسی کوین نداره!"
        )
        return

    text = "🏆 جدول ۱۰ نفر برتر\n\n"
    medals = ["🥇", "🥈", "🥉"]

    for i, (name, coins) in enumerate(ranking, 1):
        prefix = medals[i - 1] if i <= 3 else f"{i}."
        text += f"{prefix} {name} — {coins} 🪙\n"

    await update.message.reply_text(text)


# =========================
# GAME TOP
# =========================

async def gametop(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT name, best_score
        FROM game_scores
        ORDER BY best_score DESC
        LIMIT 10
    """)

    ranking = cur.fetchall()

    cur.close()
    conn.close()

    if not ranking:
        await update.message.reply_text(
            "🎮 هنوز کسی بازی نکرده!"
        )
        return

    text = "🎮 جدول رکورد Subway Bird\n\n"
    medals = ["🥇", "🥈", "🥉"]

    for i, (name, score) in enumerate(ranking, 1):
        prefix = medals[i - 1] if i <= 3 else f"{i}."
        text += f"{prefix} {name} — {score} امتیاز\n"

    await update.message.reply_text(text)


# =========================
# GAME STATS
# =========================

async def gamestats(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_user or not update.message:
        return

    user = update.effective_user
    user_id = user.id
    name = user.first_name or "کاربر"

    conn = get_db()
    cur = conn.cursor()

    try:

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

        db_name, last_score, games_played, best_score = result

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

    except Exception as e:

        print("GAME STATS ERROR:", e)

        await update.message.reply_text(
            "❌ خطا در گرفتن آمار."
        )

    finally:

        cur.close()
        conn.close()


# =========================
# PLAYER STATS - ADMIN
# =========================

async def playerstats(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message:
        return

    reply = update.message.reply_to_message

    if not reply:
        await update.message.reply_text(
            "❌ باید روی پیام شخص Reply کنی و بعد /playerstats رو بفرستی."
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

    conn = get_db()
    cur = conn.cursor()

    try:

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

        db_name, last_score, games_played, best_score = result

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

    except Exception as e:

        print("PLAYER STATS ERROR:", e)

        await update.message.reply_text(
            "❌ خطا در گرفتن آمار بازیکن."
        )

    finally:

        cur.close()
        conn.close()


# =========================
# START
# =========================

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


# =========================
# OLD TELEGRAM GAME DATA
# =========================

async def web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = update.effective_message

    if not message or not message.web_app_data:
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
        score = int(data.get("score", 0))
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
        data.get("game_id", "")
    ).strip()

    if not game_id:
        await message.reply_text(
            "❌ شناسه بازی وجود ندارد."
        )
        return

    conn = get_db()
    cur = conn.cursor()

    try:

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

        coins_awarded = score * COINS_PER_GAME_SCORE

        cur.execute("""
            INSERT INTO game_results
            (
                game_id,
                user_id,
                score,
                coins_awarded,
                created_at
            )
            VALUES (%s, %s, %s, %s, %s)
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
            VALUES (%s, %s, %s, 1, %s)

            ON CONFLICT (user_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                score = EXCLUDED.score,
                games_played =
                    game_scores.games_played + 1,
                best_score = GREATEST(
                    game_scores.best_score,
                    EXCLUDED.score
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
            VALUES (%s, %s, %s, 0)

            ON CONFLICT (user_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                coins =
                    users.coins + EXCLUDED.coins
        """, (
            user_id,
            name,
            coins_awarded
        ))

        conn.commit()

    except Exception as e:

        conn.rollback()

        print("GAME ERROR:", e)

        await message.reply_text(
            "❌ خطا در ثبت بازی."
        )
        return

    finally:
        cur.close()
        conn.close()

    await message.reply_text(
        f"🎮 بازی تموم شد!\n\n"
        f"🏆 امتیاز: {score}\n"
        f"🪙 جایزه: +{coins_awarded} کوین"
    )


# =========================
# ADD COINS
# =========================

async def addcoins(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message:
        return

    if not update.message.reply_to_message:
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
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ مقدار باید عدد باشد."
        )
        return

    if amount <= 0:
        return

    target = update.message.reply_to_message.from_user

    if not target:
        return

    name = target.first_name or "کاربر"

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
        f"✅ {amount} 🪙 به {name} اضافه شد."
    )


# =========================
# REMOVE COINS
# =========================

async def removecoins(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message:
        return

    if not update.message.reply_to_message:
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
        amount = int(context.args[0])
    except ValueError:
        return

    if amount <= 0:
        return

    target = update.message.reply_to_message.from_user

    if not target:
        return

    name = target.first_name or "کاربر"

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
        f"✅ {removed} 🪙 از {name} کم شد."
    )


# =========================
# SAY
# =========================

async def say(update: Update, context: ContextTypes.DEFAULT_TYPE):

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


# =========================
# SET GROUP
# =========================

async def setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):

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

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO bot_groups
        (chat_id, title)
        VALUES (%s, %s)

        ON CONFLICT (chat_id)
        DO UPDATE SET
            title = EXCLUDED.title
    """, (
        chat.id,
        title
    ))

    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text(
        f"✅ گروه «{title}» ثبت شد."
    )


# =========================
# GROUP MESSAGE
# =========================

async def groupmsg(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message:
        return

    if not context.args:
        await update.message.reply_text(
            "❌ مثال:\n/groupmsg سلام بچه‌ها 👋"
        )
        return

    text = " ".join(context.args)

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT chat_id
        FROM bot_groups
    """)

    groups = cur.fetchall()

    cur.close()
    conn.close()

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

        except Exception as e:
            print(
                "GROUP ERROR:",
                e
            )
            failed += 1

    await update.message.reply_text(
        f"📢 ارسال شد!\n\n"
        f"✅ موفق: {sent}\n"
        f"❌ ناموفق: {failed}"
    )


# =========================
# FLASK
# =========================

web = Flask(__name__)


@web.get("/")
def home():
    return "Bot API OK", 200


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
    ).strip() or "کاربر"

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

    conn = get_db()
    cur = conn.cursor()

    try:

        # جلوگیری از ثبت دوباره
        cur.execute("""
            SELECT 1
            FROM game_results
            WHERE game_id = %s
        """, (game_id,))

        if cur.fetchone():

            conn.rollback()

            return jsonify({
                "ok": False,
                "error": "already registered"
            }), 409

        coins_awarded = (
            score * COINS_PER_GAME_SCORE
        )

        # نتیجه بازی
        cur.execute("""
            INSERT INTO game_results
            (
                game_id,
                user_id,
                score,
                coins_awarded,
                created_at
            )
            VALUES (%s, %s, %s, %s, %s)
        """, (
            game_id,
            user_id,
            score,
            coins_awarded,
            time.time()
        ))

        # رکورد
        cur.execute("""
            INSERT INTO game_scores
            (
                user_id,
                name,
                score,
                games_played,
                best_score
            )
            VALUES (%s, %s, %s, 1, %s)

            ON CONFLICT (user_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                score = EXCLUDED.score,
                games_played =
                    game_scores.games_played + 1,
                best_score = GREATEST(
                    game_scores.best_score,
                    EXCLUDED.score
                )

            RETURNING best_score
        """, (
            user_id,
            name,
            score,
            score
        ))

        best_score = cur.fetchone()[0]

        # اضافه کردن جایزه به موجودی
        cur.execute("""
            INSERT INTO users
            (
                user_id,
                name,
                coins,
                last_message
            )
            VALUES (%s, %s, %s, 0)

            ON CONFLICT (user_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                coins = users.coins + EXCLUDED.coins
        """, (
            user_id,
            name,
            coins_awarded
        ))

        conn.commit()

        return jsonify({
            "ok": True,
            "score": score,
            "coins_awarded": coins_awarded,
            "best_score": best_score
        })

    except Exception as e:

        conn.rollback()

        print(
            "GAME API ERROR:",
            e
        )

        return jsonify({
            "ok": False,
            "error": "database error"
        }), 500

    finally:

        cur.close()
        conn.close()


def run_web():

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    web.run(
        host="0.0.0.0",
        port=port,
        use_reloader=False
    )


# =========================
# MAIN
# =========================

def main():

    init_db()

    Thread(
        target=run_web,
        daemon=True
    ).start()

    app = (
        Application
        .builder()
        .token(TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("balance", balance)
    )

    app.add_handler(
        CommandHandler("top", top)
    )

    app.add_handler(
        CommandHandler("gametop", gametop)
    )

    app.add_handler(
        CommandHandler("gamestats", gamestats)
    )

    app.add_handler(
        CommandHandler("playerstats", playerstats)
    )

    app.add_handler(
        CommandHandler("addcoins", addcoins)
    )

    app.add_handler(
        CommandHandler("removecoins", removecoins)
    )

    app.add_handler(
        CommandHandler("say", say)
    )

    app.add_handler(
        CommandHandler("setgroup", setgroup)
    )

    app.add_handler(
        CommandHandler("groupmsg", groupmsg)
    )

    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.WEB_APP_DATA,
            web_app_data
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_handler
        )
    )

    print("🤖 ربات روشن شد...")

    app.run_polling()


if __name__ == "__main__":
    main()