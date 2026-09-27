import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, ChatPermissions
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = os.environ.get("BOT_TOKEN")


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, *args):
        pass


def run_web():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()


# ─── الأوامر ─────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً! أنا بوت إدارة المجموعات.\n\n"
        "الأوامر:\n"
        "/ban — حظر (رد على رسالة)\n"
        "/mute — كتم (رد على رسالة)\n"
        "/warn — تحذير (رد على رسالة)\n"
        "/stats — إحصائيات\n"
        "/id — عرض ID"
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    await update.message.reply_text(
        f"🆔 ID تاعك: `{user.id}`\n"
        f"🆔 ID المجموعة: `{chat.id}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ رد على رسالة العضو أول.")
        return
    target = update.message.reply_to_message.from_user
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        await update.message.reply_text(
            f"🚫 {target.mention_html()} محظور.",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        await update.message.reply_text(f"❌ فشل: {e}")


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ رد على رسالة العضو أول.")
        return
    target = update.message.reply_to_message.from_user
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id,
            target.id,
            permissions=ChatPermissions(can_send_messages=False),
        )
        await update.message.reply_text(
            f"🔇 {target.mention_html()} مكتوم.",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        await update.message.reply_text(f"❌ فشل: {e}")


async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ رد على رسالة العضو أول.")
        return
    target = update.message.reply_to_message.from_user
    await update.message.reply_text(
        f"⚠️ {target.mention_html()} — تحذير!",
        parse_mode=ParseMode.HTML,
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    try:
        count = await context.bot.get_chat_member_count(chat.id)
        await update.message.reply_text(
            f"📊 إحصائيات المجموعة:\n"
            f"الاسم: {chat.title}\n"
            f"عدد الأعضاء: {count}"
        )
    except TelegramError as e:
        await update.message.reply_text(f"❌ فشل: {e}")


# ─── التشغيل ────────────────────────────────────────

def main():
    threading.Thread(target=run_web, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("warn", cmd_warn))
    app.add_handler(CommandHandler("stats", cmd_stats))

    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
