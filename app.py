import os
import threading
import sqlite3
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, ChatPermissions
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ═══════════════════════════════════════════════════
#  الإعدادات
# ═══════════════════════════════════════════════════

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DB_PATH = "/tmp/bot.db"
WARN_LIMIT = 3
FLOOD_LIMIT = 5
FLOOD_WINDOW = 10

# ═══════════════════════════════════════════════════
#  الكلمات الممنوعة (افتراضي + مخصص)
# ═══════════════════════════════════════════════════

BANNED_WORDS = [
    # عربي
    "كلب", "حمار", "خنزير", "غبي", "احمق", "أحمق", "حثالة",
    "زبالة", "قذر", "وسخ", "تافه", "حقير", "لعنة",
    "زبال", "معتوه", "مجنون", "خرف", "بغل", "نتن",
    "خرا", "كس", "طيز", "شرموط", "قحبة", "عاهر",
    "منيوك", "متناك", "عرص", "ديوث", "خول",
    # إنجليزي
    "fuck", "shit", "bitch", "asshole", "bastard", "damn",
    "cunt", "dick", "pussy", "whore", "slut", "retard",
    "idiot", "stupid", "moron", "crap", "piss", "faggot",
    "nigger", "nigga", "motherfucker",
]

WHITELIST = ["كلب البحر", "كلب الحراسة"]


# ═══════════════════════════════════════════════════
#  Web server (لـ Render)
# ═══════════════════════════════════════════════════

class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, *args):
        pass


def run_web():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()


# ═══════════════════════════════════════════════════
#  قاعدة البيانات
# ═══════════════════════════════════════════════════

class Database:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self._create_tables()

    def _create_tables(self):
        with self.lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id INTEGER PRIMARY KEY,
                    welcome TEXT DEFAULT 'أهلاً {name} في {chat}! 🌹',
                    rules TEXT DEFAULT 'لا توجد قواعد بعد.',
                    anti_links INTEGER DEFAULT 1,
                    anti_flood INTEGER DEFAULT 1
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
                CREATE TABLE IF NOT EXISTS messages (
                    chat_id INTEGER,
                    user_id INTEGER,
                    ts INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_msg
                    ON messages(chat_id, user_id, ts);
                CREATE TABLE IF NOT EXISTS stats (
                    chat_id INTEGER,
                    user_id INTEGER,
                    messages INTEGER DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id)
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

    def update_chat(self, chat_id, key, value):
        allowed = ("welcome", "rules", "anti_links", "anti_flood")
        if key not in allowed:
            return
        with self.lock:
            self.get_chat(chat_id)
            self.conn.execute(
                f"UPDATE chats SET {key} = ? WHERE chat_id = ?",
                (value, chat_id),
            )
            self.conn.commit()

    def toggle(self, chat_id, key):
        allowed = ("anti_links", "anti_flood")
        if key not in allowed:
            return 0
        with self.lock:
            self.get_chat(chat_id)
            self.conn.execute(
                f"UPDATE chats SET {key} = 1 - {key} WHERE chat_id = ?",
                (chat_id,),
            )
            self.conn.commit()
            cur = self.conn.execute(
                f"SELECT {key} FROM chats WHERE chat_id = ?", (chat_id,)
            )
            return cur.fetchone()[key]

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
                "SELECT count FROM warns "
                "WHERE chat_id = ? AND user_id = ?",
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

    def del_word(self, chat_id, word):
        with self.lock:
            self.conn.execute(
                "DELETE FROM banned_words WHERE chat_id = ? AND word = ?",
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

    def log_message(self, chat_id, user_id):
        with self.lock:
            now = int(time.time())
            self.conn.execute(
                "INSERT INTO messages VALUES (?, ?, ?)",
                (chat_id, user_id, now),
            )
            self.conn.execute("""
                INSERT INTO stats (chat_id, user_id, messages)
                VALUES (?, ?, 1)
                ON CONFLICT(chat_id, user_id) DO UPDATE
                SET messages = messages + 1
            """, (chat_id, user_id))
            self.conn.commit()

    def count_recent(self, chat_id, user_id, window):
        with self.lock:
            since = int(time.time()) - window
            cur = self.conn.execute(
                "SELECT COUNT(*) as c FROM messages "
                "WHERE chat_id = ? AND user_id = ? AND ts >= ?",
                (chat_id, user_id, since),
            )
            return cur.fetchone()["c"]

    def cleanup_messages(self):
        with self.lock:
            cutoff = int(time.time()) - 3600
            self.conn.execute(
                "DELETE FROM messages WHERE ts < ?", (cutoff,)
            )
            self.conn.commit()

    def top_users(self, chat_id, limit=10):
        with self.lock:
            cur = self.conn.execute(
                "SELECT user_id, messages FROM stats "
                "WHERE chat_id = ? ORDER BY messages DESC LIMIT ?",
                (chat_id, limit),
            )
            return cur.fetchall()


db = Database(DB_PATH)


# ═══════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════

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


def contains_banned(text, words):
    filtered = text
    for allow in WHITELIST:
        filtered = filtered.replace(allow, "")
    for w in words:
        if w in filtered:
            return w
    return None


def extract_text(msg):
    """يستخرج النص + يكتشف الروابط المخفية في entities."""
    text = (msg.text or msg.caption or "").lower()
    has_link = False

    if msg.entities:
        for ent in msg.entities:
            if ent.type in ("url", "text_link"):
                has_link = True
                break

    return text, has_link


# ═══════════════════════════════════════════════════
#  أوامر
# ═══════════════════════════════════════════════════

async def cmd_start(update, context):
    await update.message.reply_text(
        "🤖 <b>بوت إدارة المجموعات</b>\n\n"
        "<b>الإدارة:</b>\n"
        "/ban /unban /kick /mute /unmute — (رد)\n"
        "/warn /resetwarn — (رد)\n\n"
        "<b>الإعدادات:</b>\n"
        "/setwelcome &lt;نص&gt;\n"
        "/setrules &lt;نص&gt;\n"
        "/togglelinks /toggleflood\n\n"
        "<b>الكلمات:</b>\n"
        "/addword /delword /words\n\n"
        "<b>عام:</b>\n"
        "/id /stats /top /rules",
        parse_mode=ParseMode.HTML,
    )


async def cmd_id(update, context):
    u = update.effective_user
    c = update.effective_chat
    await update.message.reply_text(
        f"🆔 ID تاعك: <code>{u.id}</code>\n"
        f"🆔 ID المجموعة: <code>{c.id}</code>",
        parse_mode=ParseMode.HTML,
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
            f"✅ {t.mention_html()} فُك حظره.",
            parse_mode=ParseMode.HTML,
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
            update.effective_chat.id, t.id,
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
            update.effective_chat.id, t.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
        await update.message.reply_text(
            f"🔊 {t.mention_html()} فُك كتمه.",
            parse_mode=ParseMode.HTML,
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
                f"🚫 {t.mention_html()} حُظر بعد "
                f"{WARN_LIMIT} تحذيرات.",
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
    db.update_chat(
        update.effective_chat.id, "welcome", " ".join(context.args)
    )
    await update.message.reply_text("✅ حُفظت رسالة الترحيب.")


async def cmd_setrules(update, context):
    if not await require_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text(
            "استخدام: /setrules القاعدة 1 | القاعدة 2"
        )
        return
    db.update_chat(
        update.effective_chat.id, "rules", " ".join(context.args)
    )
    await update.message.reply_text("✅ حُفظت القواعد.")


async def cmd_rules(update, context):
    s = db.get_chat(update.effective_chat.id)
    await update.message.reply_text(
        f"📜 <b>قواعد المجموعة</b>\n\n{s['rules']}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_toggle(update, context, key, label):
    if not await require_admin(update, context):
        return
    val = db.toggle(update.effective_chat.id, key)
    status = "✅ مفعّل" if val else "❌ موقّف"
    await update.message.reply_text(f"{label}: {status}")


async def cmd_togglelinks(update, context):
    await cmd_toggle(update, context, "anti_links", "منع الروابط")


async def cmd_toggleflood(update, context):
    await cmd_toggle(update, context, "anti_flood", "منع الفلود")


async def cmd_addword(update, context):
    if not await require_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("استخدام: /addword كلمة")
        return
    for w in context.args:
        db.add_word(update.effective_chat.id, w)
    await update.message.reply_text(
        f"✅ أُضيفت: {' '.join(context.args)}"
    )


async def cmd_delword(update, context):
    if not await require_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("استخدام: /delword كلمة")
        return
    for w in context.args:
        db.del_word(update.effective_chat.id, w)
    await update.message.reply_text(
        f"✅ حُذفت: {' '.join(context.args)}"
    )


async def cmd_words(update, context):
    if not await require_admin(update, context):
        return
    words = db.get_words(update.effective_chat.id)
    if not words:
        await update.message.reply_text(
            "لا توجد كلمات مخصصة.\n"
            "(القائمة الافتراضية تحتوي على كلمات السب الشائعة)"
        )
        return
    await update.message.reply_text(
        "الكلمات المخصصة:\n" + "\n".join(words)
    )


async def cmd_stats(update, context):
    c = update.effective_chat
    try:
        count = await context.bot.get_chat_member_count(c.id)
        await update.message.reply_text(
            f"📊 <b>{c.title}</b>\nعدد الأعضاء: {count}",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        await update.message.reply_text(f"❌ {e}")


async def cmd_top(update, context):
    rows = db.top_users(update.effective_chat.id, 10)
    if not rows:
        await update.message.reply_text("لا توجد إحصائيات بعد.")
        return
    lines = ["🏆 <b>أنشط الأعضاء</b>\n"]
    for i, r in enumerate(rows, 1):
        try:
            m = await context.bot.get_chat_member(
                update.effective_chat.id, r["user_id"]
            )
            name = m.user.full_name
        except TelegramError:
            name = f"user_{r['user_id']}"
        lines.append(f"{i}. {name} — {r['messages']}")
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML
    )


# ═══════════════════════════════════════════════════
#  الترحيب
# ═══════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════
#  الحماية التلقائية
# ═══════════════════════════════════════════════════

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

    s = db.get_chat(chat.id)
    db.log_message(chat.id, user.id)

    # استخراج النص + كشف الروابط من entities
    text, has_link_entity = extract_text(msg)

    if not text and not has_link_entity:
        return

    # ─── منع الفلود ───
    if s["anti_flood"]:
        count = db.count_recent(chat.id, user.id, FLOOD_WINDOW)
        if count > FLOOD_LIMIT:
            try:
                await msg.delete()
                await context.bot.send_message(
                    chat.id,
                    f"⚠️ {user.mention_html()} — اهدأ شوي!",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass
            return

    # ─── منع السب ───
    all_banned = list(set(db.get_words(chat.id) + BANNED_WORDS))
    found = contains_banned(text, all_banned)

    if found:
        try:
            await msg.delete()
        except TelegramError:
            pass

        count = db.add_warn(chat.id, user.id)

        if count >= WARN_LIMIT:
            try:
                await context.bot.ban_chat_member(chat.id, user.id)
                db.reset_warn(chat.id, user.id)
                await context.bot.send_message(
                    chat.id,
                    f"🚫 {user.mention_html()} حُظر بعد "
                    f"{WARN_LIMIT} تحذيرات (سب).",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass
        else:
            try:
                await context.bot.send_message(
                    chat.id,
                    f"⚠️ {user.mention_html()} — ممنوع السب! "
                    f"تحذير {count}/{WARN_LIMIT}.",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass
        return

    # ─── منع الروابط ───
    if s["anti_links"]:
        has_link_text = (
            "http://" in text
            or "https://" in text
            or "t.me/" in text
            or "www." in text
        )
        if has_link_text or has_link_entity:
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


# ═══════════════════════════════════════════════════
#  المهام الدورية
# ═══════════════════════════════════════════════════

async def cleanup_job(context):
    db.cleanup_messages()


# ═══════════════════════════════════════════════════
#  التشغيل
# ═══════════════════════════════════════════════════

def main():
    threading.Thread(target=run_web, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # أوامر
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("kick", cmd_kick))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("warn", cmd_warn))
    app.add_handler(CommandHandler("resetwarn", cmd_resetwarn))
    app.add_handler(CommandHandler("setwelcome", cmd_setwelcome))
    app.add_handler(CommandHandler("setrules", cmd_setrules))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("togglelinks", cmd_togglelinks))
    app.add_handler(CommandHandler("toggleflood", cmd_toggleflood))
    app.add_handler(CommandHandler("addword", cmd_addword))
    app.add_handler(CommandHandler("delword", cmd_delword))
    app.add_handler(CommandHandler("words", cmd_words))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("top", cmd_top))

    # الأعضاء الجدد
    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_member
    ))

    # كل الرسائل الأخرى
    app.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND, on_message
    ))

    # تنظيف دوري
    app.job_queue.run_repeating(cleanup_job, interval=3600, first=60)

    print("Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
        
