import os
import threading
import sqlite3
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, ChatPermissions
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DB_PATH = "bot.db"
WARN_LIMIT = 3


# ─── Health check ──────────────────────────────────
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


# ─── Database ──────────────────────────────────────
class DB:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self._init()

    def _init(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                welcome TEXT DEFAULT 'أهلاً {name} في {chat}!',
                anti_links INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS warns (
                chat_id INTEGER,
                user_id INTEGER,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS banned_words (
                chat_id INTEGER,
                word TEXT,
                PRIMARY KEY (chat_id, word)
            );
        """)
        self.conn.commit()

    def get_chat(self, chat_id):
        with self.lock:
            cur = self.conn.execute(
                "SELECT * FROM chats WHERE chat_id = ?", (chat_id,)
            )
            row = cur.fetchone()
            if not row:
                self.conn.execute(
                    "INSERT INTO chats (chat_id) VALUES (?)", (chat_id,)
                )
                self.conn.commit()
                cur = self.conn.execute(
                    "SELECT * FROM chats WHERE chat_id = ?", (chat_id,)
                )
                row = cur.fetchone()
            return row

    def set_welcome(self, chat_id, text):
        with self.lock:
            self.get_chat(chat_id)
            self.conn.execute(
                "UPDATE chats SET welcome = ? WHERE chat_id = ?",
                (text, chat_id),
            )
            self.conn.commit()

    def toggle_links(self, chat_id):
        with self.lock:
            self.get_chat(chat_id)
            self.conn.execute(
                "UPDATE chats SET anti_links = 1 - anti_links "
                "WHERE chat_id = ?",
                (chat_id,),
            )
            self.conn.commit()

    def add_warn(self, chat_id, user_id):
        with self.lock:
            self.conn.execute("""
                INSERT INTO warns (chat_id, user_id, count)
                VALUES (?, ?, 1)
                ON CONFLICT(chat_id, user_id) DO UPDATE
                SET count = count + 1
            """, (chat_id, user_id))
            self.conn.commit()
            cur = self.conn.execute(
                "SELECT count FROM warns WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )
            return cur.fetchone()["count"]

    def reset_warn(self, chat_id, user_id):
        with self.lock:
            self.conn.execute(
                "DELETE FROM warns WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )
            self.conn.commit()

    def add_word(self, chat_id, word):
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO banned_words VALUES (?, ?)",
                (chat_id, word.lower()),
            )
            self.conn.commit()

    def get_words(self, chat_id):
        with self.lock:
            cur = self.conn.execute(
                "SELECT word FROM banned_words WHERE chat_id = ?",
                (chat_id,),
            )
            return [r["word"] for r in cur.fetchall()]


db = DB()


# ─── Helpers ───────────────────────────────────────
async def is_admin(context, chat_id, user_id):
    try:
        m = await context.bot.get_chat_member(chat_id, user_id)
        return m.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except TelegramError:
        return False


async def require_admin(update, context):
    if not await is_admin(
        context, update.effective_chat.id, update.effective_user.id
    ):
        await update.message.reply_text("❌ للمشرفين فقط.")
        return False
    return True


# ─── Commands ──────────────────────────────────────
async def start(update, context):
    await update.message.reply_text(
        "🤖 بوت إدارة المجموعات\n\n"
        "الأوامر:\n"
        "/id — عرض ID\n"
        "/ban — حظر (رد)\n"
        "/unban — فك الحظر (رد)\n"
        "/kick — طرد (رد)\n"
        "/mute — كتم (رد)\n"
        "/unmute — فك الكتم (رد)\n"
        "/warn — تحذير (رد)\n"
        "/resetwarn — تصفير التحذيرات (رد)\n"
        "/setwelcome <نص> — رسالة الترحيب\n"
        "/togglelinks — تشغيل/إيقاف منع الروابط\n"
        "/addword <كلمة> — إضافة كلمة ممنوعة\n"
        "/words — عرض الكلمات الممنوعة\n"
        "/stats — إحصائيات"
    )


async def cmd_id(update, context):
    u = update.effective_user
    c = update.effective_chat
    await update.message.reply_text(
        f"🆔 ID تاعك: `{u.id}`\n🆔 ID المجموعة: `{c.id}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_ban(update, context):
    if not await require_admin(update, context):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ رد على رسالة العضو.")
        return
    t = update.message.reply_to_message.from_user
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, t.id)
        await update.message.reply_text(
            f"🚫 {t.mention_html()} محظور.", parse_mode=ParseMode.HTML
        )
    except TelegramError as e:
        await update.message.reply_text(f"❌ {e}")


async def cmd_unban(update, context):
    if not await require_admin(update, context):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ رد على رسالة العضو.")
        return
    t = update.message.reply_to_message.from_user
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, t.id)
        await update.message.reply_text(
            f"✅ {t.mention_html()} فُك حظره.", parse_mode=ParseMode.HTML
        )
    except TelegramError as e:
        await update.message.reply_text(f"❌ {e}")


async def cmd_kick(update, context):
    if not await require_admin(update, context):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ رد على رسالة العضو.")
        return
    t = update.message.reply_to_message.from_user
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, t.id)
        await context.bot.unban_chat_member(update.effective_chat.id, t.id)
        await update.message.reply_text(
            f"👢 {t.mention_html()} طُرد.", parse_mode=ParseMode.HTML
        )
    except TelegramError as e:
        await update.message.reply_text(f"❌ {e}")


async def cmd_mute(update, context):
    if not await require_admin(update, context):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ رد على رسالة العضو.")
        return
    t = update.message.reply_to_message.from_user
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id,
            t.id,
            permissions=ChatPermissions(can_send_messages=False),
        )
        await update.message.reply_text(
            f"🔇 {t.mention_html()} مكتوم.", parse_mode=ParseMode.HTML
        )
    except TelegramError as e:
        await update.message.reply_text(f"❌ {e}")


async def cmd_unmute(update, context):
    if not await require_admin(update, context):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ رد على رسالة العضو.")
        return
    t = update.message.reply_to_message.from_user
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id,
            t.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
        await update.message.reply_text(
            f"🔊 {t.mention_html()} فُك كتمه.", parse_mode=ParseMode.HTML
        )
    except TelegramError as e:
        await update.message.reply_text(f"❌ {e}")


async def cmd_warn(update, context):
    if not await require_admin(update, context):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ رد على رسالة العضو.")
        return
    t = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id
    count = db.add_warn(chat_id, t.id)

    if count >= WARN_LIMIT:
        try:
            await context.bot.ban_chat_member(chat_id, t.id)
            db.reset_warn(chat_id, t.id)
            await update.message.reply_text(
                f"🚫 {t.mention_html()} حُظر بعد {WARN_LIMIT} تحذيرات.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as e:
            await update.message.reply_text(f"❌ {e}")
    else:
        await update.message.reply_text(
            f"⚠️ {t.mention_html()} — تحذير {count}/{WARN_LIMIT}.",
            parse_mode=ParseMode.HTML,
        )


async def cmd_resetwarn(update, context):
    if not await require_admin(update, context):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ رد على رسالة العضو.")
        return
    t = update.message.reply_to_message.from_user
    db.reset_warn(update.effective_chat.id, t.id)
    await update.message.reply_text(
        f"✅ تحذيرات {t.mention_html()} صُفّرت.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_setwelcome(update, context):
    if not await require_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text(
            "استخدام: /setwelcome أهلاً {name} في {chat}"
        )
        return
    text = " ".join(context.args)
    db.set_welcome(update.effective_chat.id, text)
    await update.message.reply_text("✅ حُفظت رسالة الترحيب.")


async def cmd_togglelinks(update, context):
    if not await require_admin(update, context):
        return
    db.toggle_links(update.effective_chat.id)
    s = db.get_chat(update.effective_chat.id)
    status = "✅ مفعّل" if s["anti_links"] else "❌ موقّف"
    await update.message.reply_text(f"منع الروابط: {status}")


async def cmd_addword(update, context):
    if not await require_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("استخدام: /addword كلمة")
        return
    for w in context.args:
        db.add_word(update.effective_chat.id, w)
    await update.message.reply_text(f"✅ أُضيفت: {' '.join(context.args)}")


async def cmd_words(update, context):
    if not await require_admin(update, context):
        return
    words = db.get_words(update.effective_chat.id)
    if not words:
        await update.message.reply_text("لا توجد كلمات ممنوعة.")
        return
    await update.message.reply_text("الكلمات الممنوعة:\n" + "\n".join(words))


async def cmd_stats(update, context):
    c = update.effective_chat
    try:
        count = await context.bot.get_chat_member_count(c.id)
        await update.message.reply_text(
            f"📊 {c.title}\nعدد الأعضاء: {count}"
        )
    except TelegramError as e:
        await update.message.reply_text(f"❌ {e}")


# ─── Handlers ──────────────────────────────────────
async def on_new_member(update, context):
    msg = update.message
    if not msg or not msg.new_chat_members:
        return
    chat = update.effective_chat
    s = db.get_chat(chat.id)
    for m in msg.new_chat_members:
        if m.is_bot:
            continue
        text = s["welcome"].replace("{name}", m.first_name)
        text = text.replace("{chat}", chat.title or "")
        try:
            await context.bot.send_message(
                chat.id,
                f"👋 {m.mention_html()}\n{text}",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass


async def on_message(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return
    if chat.type not in ("group", "supergroup"):
        return
    if user.is_bot:
        return
    if await is_admin(context, chat.id, user.id):
        return

    text = (msg.text or msg.caption or "").lower()
    if not text:
        return

    s = db.get_chat(chat.id)

    # كلمات ممنوعة
    banned = db.get_words(chat.id)
    for w in banned:
        if w in text:
            try:
                await msg.delete()
                await context.bot.send_message(
                    chat.id,
                    f"⚠️ {user.mention_html()} — كلمة ممنوعة.",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass
            return

    # روابط
    if s["anti_links"]:
        if "http://" in text or "https://" in text or "t.me/" in text:
            try:
                await msg.delete()
                await context.bot.send_message(
                    chat.id,
                    f"⚠️ {user.mention_html()} — الروابط ممنوعة.",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass
            return


# ─── Main ──────────────────────────────────────────
def main():
    threading.Thread(target=run_web, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("kick", cmd_kick))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("warn", cmd_warn))
    app.add_handler(CommandHandler("resetwarn", cmd_resetwarn))
    app.add_handler(CommandHandler("setwelcome", cmd_setwelcome))
    app.add_handler(CommandHandler("togglelinks", cmd_togglelinks))
    app.add_handler(CommandHandler("addword", cmd_addword))
    app.add_handler(CommandHandler("words", cmd_words))
    app.add_handler(CommandHandler("stats", cmd_stats))

    from telegram.ext import MessageHandler, filters
    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_member
    ))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_message
    ))

    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
