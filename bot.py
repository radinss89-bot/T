import json
import time

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


TOKEN = "8594435724:AAFQvX7jg6Nc7OJ2Xpb5ZV-aK2Sm-J79Q2E"
ADMIN_ID = 6235380364

COINS_PER_MESSAGE = 10
COOLDOWN = 5 * 60
DATA_FILE = "coins.json"


def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


data = load_data()


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if not message or not message.text or not message.from_user:
        return

    if message.text.strip() != "فولک":
        return

    user = message.from_user
    user_id = str(user.id)
    name = user.first_name or "کاربر"
    now = time.time()

    user_data = data.get(user_id, {
        "coins": 0,
        "last": 0,
        "name": name
    })

    user_data["name"] = name

    if now - user_data.get("last", 0) < COOLDOWN:
        return

    user_data["coins"] = user_data.get("coins", 0) + COINS_PER_MESSAGE
    user_data["last"] = now

    data[user_id] = user_data
    save_data()

    await message.reply_text(
        f"🪙 {name} +{COINS_PER_MESSAGE} کوین گرفت!"
    )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return

    user_id = str(update.effective_user.id)
    coins = data.get(user_id, {}).get("coins", 0)

    await update.message.reply_text(
        f"💰 موجودی شما: {coins} کوین"
    )


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    ranking = sorted(
        data.values(),
        key=lambda user: user.get("coins", 0),
        reverse=True
    )[:10]

    if not ranking:
        await update.message.reply_text(
            "🏆 هنوز کسی کوین نداره!"
        )
        return

    text = "🏆 جدول ۱۰ نفر برتر\n\n"
    medals = ["🥇", "🥈", "🥉"]

    for i, user in enumerate(ranking, 1):
        name = user.get("name", "کاربر")
        coins = user.get("coins", 0)

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
    target_id = str(target.id)
    target_name = target.first_name or "کاربر"

    user_data = data.get(target_id, {
        "coins": 0,
        "last": 0,
        "name": target_name
    })

    user_data["name"] = target_name
    user_data["coins"] = user_data.get("coins", 0) + amount

    data[target_id] = user_data
    save_data()

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
    target_id = str(target.id)
    target_name = target.first_name or "کاربر"

    user_data = data.get(target_id, {
        "coins": 0,
        "last": 0,
        "name": target_name
    })

    user_data["name"] = target_name

    current_coins = user_data.get("coins", 0)
    removed = min(amount, current_coins)

    user_data["coins"] = current_coins - removed

    data[target_id] = user_data
    save_data()

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