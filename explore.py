"""
explore.py — سیستم اکسپلور برای ربات

جریان کار:
  کاربر لینک می‌فرسته  ->  ذخیره با وضعیت pending  ->  پیام بررسی برای ادمین
  ادمین ✅ / ❌ می‌زنه  ->  فقط تاییدشده‌ها تو /explore/feed میان
  ادمین خودش لینک بفرسته  ->  مستقیم تایید می‌شه

نیازمندی: python-telegram-bot v20+ ، Flask ، psycopg2
"""
import asyncio
import logging
import re
from urllib.parse import parse_qs, urlparse

from flask import jsonify, request
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

log = logging.getLogger("explore")

URL_RE = re.compile(r"https?://[^\s]+", re.I)
YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


# ───────────────────────── تشخیص نوع لینک ─────────────────────────
def parse_video(url: str):
    """(kind, ref) برمی‌گردونه یا None اگه پشتیبانی نشه."""
    try:
        u = urlparse(url)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    path = u.path or ""

    if host == "youtu.be":
        vid = path.strip("/").split("/")[0]
        return ("youtube", vid) if YT_ID.match(vid) else None

    if host in ("youtube.com", "music.youtube.com"):
        if path == "/watch":
            vid = parse_qs(u.query).get("v", [""])[0]
        else:
            m = re.match(r"^/(shorts|embed|live)/([A-Za-z0-9_-]{11})", path)
            vid = m.group(2) if m else ""
        return ("youtube", vid) if YT_ID.match(vid) else None

    if host == "aparat.com":
        m = re.match(r"^/v/([A-Za-z0-9]+)", path)
        return ("aparat", m.group(1)) if m else None

    if u.scheme == "https" and path.lower().endswith((".mp4", ".webm", ".mov")):
        return ("mp4", url)

    return None


# ───────────────────────── ثبت در ربات و Flask ─────────────────────────
def register_explore(ptb_app, flask_app, get_conn, put_conn, admin_id: int, explore_url: str):
    """
    ptb_app     : همون Application ربات (python-telegram-bot)
    flask_app   : همون Flask app (که /game-score روش هست)
    get_conn    : تابعی که یه اتصال از pool می‌گیره
    put_conn    : تابعی که اتصال رو به pool برمی‌گردونه
    admin_id    : آیدی عددی ادمین
    explore_url : آدرس صفحه‌ی explore.html (مینی‌اپ)
    """

    def q(sql, params=(), fetch=False):
        conn = get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    return cur.fetchall() if fetch else None
        finally:
            put_conn(conn)

    async def aq(*args, **kwargs):
        # مثل run_db تو کد اصلی: کوئری رو تو ترد جدا اجرا می‌کنه
        return await asyncio.to_thread(q, *args, **kwargs)

    q(
        """
        CREATE TABLE IF NOT EXISTS explore_videos (
            id SERIAL PRIMARY KEY,
            url TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            ref TEXT NOT NULL,
            submitted_by BIGINT,
            submitter_name TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    # ───── کیبورد بررسی برای ادمین ─────
    def review_kb(vid: int, url: str):
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ تایید", callback_data=f"exp:ok:{vid}"),
                    InlineKeyboardButton("❌ رد", callback_data=f"exp:no:{vid}"),
                ],
                [
                    InlineKeyboardButton("🔗 بررسی لینک", url=url),
                    InlineKeyboardButton(
                        "👀 پیش‌نمایش",
                        web_app=WebAppInfo(url=f"{explore_url}?preview={vid}"),
                    ),
                ],
            ]
        )

    # ───── دریافت لینک از کاربر ─────
    async def on_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        m = URL_RE.search(msg.text or "")
        if not m:
            return
        url = m.group(0).rstrip(").,،؛")
        parsed = parse_video(url)
        if not parsed:
            await msg.reply_text(
                "این لینک پشتیبانی نمی‌شه 🥶\n"
                "یوتیوب، آپارات یا لینک مستقیم mp4 (https) بفرست."
            )
            return

        kind, ref = parsed
        user = update.effective_user
        is_admin = user.id == admin_id
        status = "approved" if is_admin else "pending"

        rows = await aq(
            """
            INSERT INTO explore_videos (url, kind, ref, submitted_by, submitter_name, status)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (url) DO NOTHING
            RETURNING id
            """,
            (url, kind, ref, user.id, user.full_name, status),
            fetch=True,
        )
        if not rows:
            await msg.reply_text("این لینک قبلاً ثبت شده 🦅")
            return
        vid = rows[0][0]

        if is_admin:
            await msg.reply_text(f"✅ مستقیم به اکسپلور اضافه شد 😈 (#{vid})")
            return

        await msg.reply_text("ارسال شد ⏳ بعد از تایید ادمین توی اکسپلور میاد 🦅")
        await context.bot.send_message(
            admin_id,
            f"🆕 ویدیوی جدید #{vid}\n"
            f"👤 {user.full_name} (@{user.username or '-'}) | {user.id}\n"
            f"🎞 {kind}\n{url}",
            reply_markup=review_kb(vid, url),
            disable_web_page_preview=True,
        )

    # ───── تایید / رد توسط ادمین ─────
    async def on_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
        cq = update.callback_query
        if cq.from_user.id != admin_id:
            await cq.answer("فقط ادمین 😈", show_alert=True)
            return
        _, act, vid = cq.data.split(":")
        status = "approved" if act == "ok" else "rejected"
        rows = await aq(
            "UPDATE explore_videos SET status=%s WHERE id=%s RETURNING submitted_by",
            (status, int(vid)),
            fetch=True,
        )
        await cq.answer("انجام شد")
        label = "✅ تایید شد" if status == "approved" else "❌ رد شد"
        await cq.edit_message_text(
            f"{cq.message.text}\n\n{label}", disable_web_page_preview=True
        )
        if rows and rows[0][0] and rows[0][0] != admin_id:
            try:
                await context.bot.send_message(
                    rows[0][0],
                    "✅ ویدیوت تایید شد و توی اکسپلور اومد 🦅"
                    if status == "approved"
                    else "❌ ویدیوت تایید نشد 🥶",
                )
            except Exception:
                pass

    # ───── صف بررسی ─────
    async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != admin_id:
            return
        rows = await aq(
            "SELECT id, kind, url, submitter_name FROM explore_videos "
            "WHERE status='pending' ORDER BY id LIMIT 10",
            fetch=True,
        )
        if not rows:
            await update.effective_message.reply_text("صف بررسی خالیه ✅")
            return
        for vid, kind, url, name in rows:
            await update.effective_message.reply_text(
                f"🆕 #{vid} | {kind}\n👤 {name}\n{url}",
                reply_markup=review_kb(vid, url),
                disable_web_page_preview=True,
            )

    # ───── باز کردن اکسپلور ─────
    async def cmd_explore(update: Update, context: ContextTypes.DEFAULT_TYPE):
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🦅 باز کردن اکسپلور", web_app=WebAppInfo(url=explore_url))]]
        )
        await update.effective_message.reply_text("اکسپلور 😈", reply_markup=kb)

    # group=5 تا با هندلرهای دیگه‌ی ربات (مدیریت گروه و ...) تداخل نکنه
    ptb_app.add_handler(CommandHandler("explore", cmd_explore), group=5)
    ptb_app.add_handler(CommandHandler("pending", cmd_pending), group=5)
    ptb_app.add_handler(
        MessageHandler(filters.Regex(r"^اکسپلور$") & filters.ChatType.PRIVATE, cmd_explore),
        group=5,
    )
    ptb_app.add_handler(
        CallbackQueryHandler(on_review, pattern=r"^exp:(ok|no):\d+$"), group=5
    )
    ptb_app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & filters.ChatType.PRIVATE
            & filters.Regex(r"https?://"),
            on_link,
        ),
        group=5,
    )

    # ───── API برای مینی‌اپ ─────
    @flask_app.get("/explore/feed")
    def explore_feed():
        before = request.args.get("before", type=int)
        limit = max(1, min(request.args.get("limit", 6, type=int), 20))
        if before:
            rows = q(
                "SELECT id, kind, ref, url FROM explore_videos "
                "WHERE status='approved' AND id < %s ORDER BY id DESC LIMIT %s",
                (before, limit),
                fetch=True,
            )
        else:
            rows = q(
                "SELECT id, kind, ref, url FROM explore_videos "
                "WHERE status='approved' ORDER BY id DESC LIMIT %s",
                (limit,),
                fetch=True,
            )
        return jsonify(
            [{"id": r[0], "kind": r[1], "ref": r[2], "url": r[3]} for r in rows]
        )

    @flask_app.get("/explore/video/<int:vid>")
    def explore_video(vid):
        rows = q(
            "SELECT id, kind, ref, url, status FROM explore_videos WHERE id=%s",
            (vid,),
            fetch=True,
        )
        if not rows:
            return jsonify({"error": "not found"}), 404
        r = rows[0]
        return jsonify(
            {"id": r[0], "kind": r[1], "ref": r[2], "url": r[3], "status": r[4]}
        )
