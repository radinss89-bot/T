import os
 time
import json
import psycopg2

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
    return psycopg2.connect(os.environ["DATABASE_URL"])


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

    conn.commit()
    cur.close()
    conn.close()


# =========================
# USER FUNCTIONS
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
    """, (
        user_id,
        name,
        coins,
        last
    ))

    conn.commit()

    cur.close()
    conn.close()


# =========================
# فولک
# =========================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

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


# =========================
# BALANCE
# =========================

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_user:
        return

    if not update.message:
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
# TOP COINS
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

    medals = [
        "🥇",
        "🥈",
        "🥉"
    ]

    for i, (name, coins) in enumerate(
        ranking,
        1
    ):

        if i <= 3:
            prefix = medals[i - 1]
        else:
            prefix = f"{i}."

        text += (
            f"{prefix} "
            f"{name} — "
            f"{coins} 🪙\n"
        )

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

    medals = [
        "🥇",
        "🥈",
        "🥉"
    ]

    for i, (name, score) in enumerate(
        ranking,
        1
    ):

        if i <= 3:
            prefix = medals[i - 1]
        else:
            prefix = f"{i}."

        text += (
            f"{prefix} "
            f"{name} — "
            f"{score} امتیاز\n"
        )

    await update.message.reply_text(text)


# =========================
# START + MINI APP
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return

    keyboard = [
        [
            KeyboardButton(
                "🎮 بازی Subway Bird",
                web_app=WebAppInfo(
                    url=GAME_URL
                )
            )
        ]
    ]

    reply_markup = ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True
    )

    await update.message.reply_text(

        "🤖 ربات کوین فعاله!\n\n"

        "🎮 Subway Bird\n"
        "🪙 هر امتیاز بازی = ۵ کوین\n\n"

        "🗣 فولک = ۱۰ کوین\n"
        "💰 /balance = موجودی\n"
        "🏆 /top = جدول کوین‌ها\n"
        "🎮 /gametop = جدول رکورد بازی\n\n"

        "👑 دستورات ادمین:\n"
        "➕ /addcoins 1000\n"
        "➖ /removecoins 1000\n"
        "📢 /say متن",

        reply_markup=reply_markup
    )


# =========================
# RECEIVE GAME SCORE
# =========================

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

    raw_data = message.web_app_data.data

    print(
        "GAME DATA RECEIVED:",
        raw_data
    )

    # -------------------------
    # JSON
    # -------------------------

    try:

        data = json.loads(
            raw_data
        )

    except (
        json.JSONDecodeError,
        TypeError
    ):

        await message.reply_text(
            "❌ اطلاعات بازی نامعتبر است."
        )

        return

    # -------------------------
    # ACTION
    # -------------------------

    if data.get("action") != "game_over":

        await message.reply_text(
            "❌ اطلاعات بازی نامعتبر است."
        )

        return

    # -------------------------
    # SCORE
    # -------------------------

    try:

        score = int(
            data.get(
                "score",
                0
            )
        )

    except (
        TypeError,
        ValueError
    ):

        await message.reply_text(
            "❌ امتیاز نامعتبر است."
        )

        return

    # -------------------------
    # CHECK SCORE
    # -------------------------

    if score < 0:

        await message.reply_text(
            "❌ امتیاز نامعتبر است."
        )

        return

    if score > 100000:

        await message.reply_text(
            "❌ امتیاز غیرمجاز است."
        )

        return

    # -------------------------
    # GAME ID
    # -------------------------

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

    # =========================
    # DATABASE TRANSACTION
    # =========================

    conn = get_db()
    cur = conn.cursor()

    try:

        # Check duplicate game

        cur.execute("""
            SELECT 1
            FROM game_results
            WHERE game_id = %s
        """, (
            game_id,
        ))

        already_exists = cur.fetchone()

        if already_exists:

            conn.rollback()

            await message.reply_text(
                "⚠️ این نتیجه بازی قبلاً ثبت شده."
            )

            return

        # -------------------------
        # COINS
        # -------------------------

        coins_awarded = (
            score *
            COINS_PER_GAME_SCORE
        )

        now = time.time()

        # -------------------------
        # SAVE GAME
        # -------------------------

        cur.execute("""
            INSERT INTO game_results
            (
                game_id,
                user_id,
                score,
                coins_awarded,
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
            game_id,
            user_id,
            score,
            coins_awarded,
            now
        ))

        # -------------------------
        # SAVE RECORD
        # -------------------------

        cur.execute("""
            INSERT INTO game_scores
            (
                user_id,
                name,
                score,
                games_played,
                best_score
            )

            VALUES (
                %s,
                %s,
                %s,
                1,
                %s
            )

            ON CONFLICT (user_id)

            DO UPDATE SET

                name =
                    EXCLUDED.name,

                score =
                    EXCLUDED.score,

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

        # -------------------------
        # ADD COINS
        # -------------------------

        cur.execute("""
            INSERT INTO users
            (
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

                name =
                    EXCLUDED.name,

                coins =
                    users.coins
                    + EXCLUDED.coins
        """, (
            user_id,
            name,
            coins_awarded
        ))

        conn.commit()

    except Exception as e:

        conn.rollback()

        print(
            "GAME ERROR:",
            e
        )

        await message.reply_text(
            "❌ هنگام ثبت نتیجه مشکلی پیش آمد."
        )

        return

    finally:

        cur.close()
        conn.close()

    # =========================
    # SUCCESS
    # =========================

    await message.reply_text(

        f"🎮 بازی تموم شد!\n\n"

        f"🏆 امتیاز: {score}\n"

        f"🪙 جایزه: "
        f"+{coins_awarded} کوین\n\n"

        f"🎯 رکوردت رو با "
        f"/gametop ببین!"
    )


# =========================
# ADD COINS
# =========================

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

    if not update.message.reply_to_message:

        await update.message.reply_text(
            "❌ روی پیام شخص Reply کن.\n"
            "مثال: /addcoins 1000"
        )

        return

    if not context.args:

        await update.message.reply_text(
            "❌ مقدار کوین را وارد کن.\n"
            "مثال: /addcoins 1000"
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

    target = (
        update
        .message
        .reply_to_message
        .from_user
    )

    target_id = target.id
    target_name = target.first_name or "کاربر"

    coins, last = get_user(
        target_id,
        target_name
    )

    coins += amount

    update_user(
        target_id,
        target_name,
        coins,
        last
    )

    await update.message.reply_text(
        f"✅ به {target_name} "
        f"تعداد {amount} 🪙 کوین اضافه شد!"
    )


# =========================
# REMOVE COINS
# =========================

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

    if not update.message.reply_to_message:

        await update.message.reply_text(
            "❌ روی پیام شخص Reply کن.\n"
            "مثال: /removecoins 1000"
        )

        return

    if not context.args:

        await update.message.reply_text(
            "❌ مقدار کوین را وارد کن.\n"
            "مثال: /removecoins 1000"
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

    target = (
        update
        .message
        .reply_to_message
        .from_user
    )

    target_id = target.id
    target_name = target.first_name or "کاربر"

    coins, last = get_user(
        target_id,
        target_name
    )

    removed = min(
        amount,
        coins
    )

    coins -= removed

    update_user(
        target_id,
        target_name,
        coins,
        last
    )

    await update.message.reply_text(
        f"✅ از {target_name} "
        f"تعداد {removed} 🪙 کوین کم شد!"
    )


# =========================
# SAY
# =========================

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

        await update.message.reply_text(
            "❌ متن پیام را وارد کن.\n"
            "مثال: /say سلام بچه‌ها 👋"
        )

        return

    text = " ".join(
        context.args
    )

    await update.message.reply_text(
        text
    )


# =========================
# MAIN
# =========================

def main():

    init_db()

    app = (
        Application
        .builder()
        .token(TOKEN)
        .build()
    )

    # Commands

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CommandHandler(
            "balance",
            balance
        )
    )

    app.add_handler(
        CommandHandler(
            "top",
            top
        )
    )

    app.add_handler(
        CommandHandler(
            "gametop",
            gametop
        )
    )

    app.add_handler(
        CommandHandler(
            "addcoins",
            addcoins
        )
    )

    app.add_handler(
        CommandHandler(
            "removecoins",
            removecoins
        )
    )

    app.add_handler(
        CommandHandler(
            "say",
            say
        )
    )

    # Mini App data

    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.WEB_APP_DATA,
            web_app_data
        )
    )

    # فولک

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_handler
        )
    )

    print(
        "🤖 ربات روشن شد..."
    )

    app.run_polling()


# =========================
# RUN
# =========================

if __name__ == "__main__":
    main()