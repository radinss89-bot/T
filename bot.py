import os
import time
import psycopg2
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = 6235380364

COINS_PER_MESSAGE = 10
COOLDOWN = 2 * 60


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

    conn.commit()
    cur.close()
    conn.close()


def get_user(user_id, name):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        "SELECT coins, last_message FROM users WHERE user_id = %s",
        (user_id,)
    )

    row = cur.fetchone()

    if row is None:
        cur.execute(
            """
            INSERT INTO users (user_id, name, coins, last_message)
            VALUES (%s, %s, 0, 0)
            """,
            (user_id, name)
        )
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

    cur.execute(
        """
        INSERT INTO users (user_id, name, coins, last_message)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id)
        DO UPDATE SET
            name = EXCLUDED.name,
            coins = EXCLUDED.coins,
            last_message = EXCLUDED.last_message
        """,
        (user_id, name, coins, last)
    )

    conn.commit()
    cur.close()
    conn.close()


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


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return

    user = update.effective_user
    name = user.first_name or "کاربر"

    coins, _ = get_user(user.id, name)

    await update.message.reply_text(
        f"💰 موجودی شما: {coins} کوین"
    )


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    await update.message.reply_text(
        "🤖 ربات کوین فعاله!\n\n"
        "🗣 فولک = ۱۰ کوین هر ۵ دقیقه\n"
        "💰 /balance = موجودی\n"
        "🏆 /top = جدول برترین‌ها\n"
        "➕ /addcoins 1000 = اضافه کردن کوین\n"
        "➖ /removecoins 1000 = کم کردن کوین\n"
        "📢 /say متن = ارسال پیام توسط ادمین"
    )


async def addcoins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message or not update.message.reply_to_message:
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
        amount = int(context.args[0])
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

    target = update.message.reply_to_message.from_user

    target_id = target.id
    target_name = target.first_name or "کاربر"

    coins, last = get_user(target_id, target_name)

    coins += amount

    update_user(
        target_id,
        target_name,
        coins,
        last
    )

    await update.message.reply_text(
        f"✅ به {target_name} تعداد {amount} 🪙 کوین اضافه شد!"
    )


async def removecoins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message or not update.message.reply_to_message:
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
        amount = int(context.args[0])
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

    target = update.message.reply_to_message.from_user

    target_id = target.id
    target_name = target.first_name or "کاربر"

    coins, last = get_user(target_id, target_name)

    removed = min(amount, coins)
    coins -= removed

    update_user(
        target_id,
        target_name,
        coins,
        last
    )

    await update.message.reply_text(
        f"✅ از {target_name} تعداد {removed} 🪙 کوین کم شد!"
    )


async def say(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    text = " ".join(context.args)

    await update.message.reply_text(text)


def main():
    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("addcoins", addcoins))
    app.add_handler(CommandHandler("removecoins", removecoins))
    app.add_handler(CommandHandler("say", say))

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