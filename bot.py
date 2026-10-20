#!/usr/bin/env python3
"""Telegram channel footer bot. Python 3.10+, python-telegram-bot 22.5."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import sqlite3
from typing import Any

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from telegram import Message, MessageEntity, Update
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

BASE = Path(__file__).resolve().parent
LOG = logging.getLogger("rights_bot")
DEFAULTS = {"enabled": True, "footer_on": True, "footer": "جميع الحقوق محفوظة ©",
            "channel_button": False, "channel_url": "", "likes": False, "clean": False}
LABELS = {"enabled": "تشغيل البوت بالقناة", "footer_on": "إضافة الحقوق",
          "channel_button": "زر اسم القناة ورابطها", "likes": "لايك ودس لايك",
          "clean": "حذف الروابط والمعرّفات من النص"}
OWNER_ID = 0
DB: sqlite3.Connection | None = None


def load_env() -> None:
    """Read simple KEY=value config; never overwrite real environment variables."""
    path = BASE / ".env"
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if raw and not raw.startswith("#") and "=" in raw:
                key, value = raw.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def init_db(path: Path) -> None:
    global DB
    DB = sqlite3.connect(path)
    DB.row_factory = sqlite3.Row
    DB.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, config TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS processed (
            chat INTEGER NOT NULL, msg INTEGER NOT NULL,
            PRIMARY KEY(chat,msg));
        CREATE TABLE IF NOT EXISTS albums (
            chat INTEGER NOT NULL, album TEXT NOT NULL,
            PRIMARY KEY(chat,album));
        CREATE TABLE IF NOT EXISTS posts (
            chat INTEGER NOT NULL, msg INTEGER NOT NULL, keyboard TEXT NOT NULL,
            PRIMARY KEY(chat,msg));
        CREATE TABLE IF NOT EXISTS votes (
            chat INTEGER NOT NULL, msg INTEGER NOT NULL,
            user INTEGER NOT NULL, value INTEGER NOT NULL,
            PRIMARY KEY(chat,msg,user));
    """)
    DB.commit()


def channel(cid: int) -> dict[str, Any] | None:
    row = DB.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
    if not row:
        return None
    return {"id": cid, "title": row["title"], **DEFAULTS, **json.loads(row["config"])}


def save_channel(c: dict[str, Any]) -> None:
    config = {key: c[key] for key in DEFAULTS}
    with DB:
        DB.execute("INSERT OR REPLACE INTO channels VALUES (?,?,?)",
                   (c["id"], c["title"], json.dumps(config, ensure_ascii=False)))


def u16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def clean_entities(text: str, entities) -> tuple[str, list[MessageEntity]]:
    """Remove detected URLs/mentions (including linked labels), retaining other formatting."""
    intervals = sorted((e.offset, e.offset + e.length) for e in entities
                       if e.type in ("url", "text_link", "mention"))
    merged: list[list[int]] = []
    for a, b in intervals:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    if not merged:
        return text, list(entities)
    raw = text.encode("utf-16-le")
    pieces = []
    cursor = 0
    for a, b in merged:
        pieces.append(raw[cursor * 2:a * 2])
        cursor = b
    pieces.append(raw[cursor * 2:])

    def mapped(pos: int) -> int:
        return pos - sum(max(0, min(pos, b) - a) for a, b in merged if pos > a)

    adjusted = []
    for entity in entities:
        if entity.type in ("url", "text_link", "mention"):
            continue
        a, b = mapped(entity.offset), mapped(entity.offset + entity.length)
        if b > a:
            data = entity.to_dict()
            data.update(offset=a, length=b-a)
            adjusted.append(MessageEntity.de_json(data, None))
    return b"".join(pieces).decode("utf-16-le"), adjusted


def compose(text: str, entities, config: dict, limit: int):
    result, ents = (clean_entities(text, entities) if config["clean"] else (text, list(entities)))
    footer = config["footer"].strip()
    if config["footer_on"] and footer and not (result == footer or result.endswith("\n\n" + footer)):
        result += ("\n\n" if result else "") + footer
    # Conservative UTF-16 check: never cut original text or media captions.
    if u16(result) > limit or not result:
        return None
    return result, ents


def is_owner(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == OWNER_ID
                and update.effective_chat and update.effective_chat.type == "private")


async def authorized_channel(bot, cid: int) -> bool:
    try:
        member = await bot.get_chat_member(cid, OWNER_ID)
        return member.status in ("creator", "administrator")
    except TelegramError:
        return False


async def notify(context, text: str) -> None:
    try:
        await context.bot.send_message(OWNER_ID, text)
    except TelegramError:
        LOG.warning("Could not notify owner")


def settings_keyboard(c: dict) -> Markup:
    rows = [[Button(f"{LABELS[key]}: {'✅' if c[key] else '❌'}",
                    callback_data=f"toggle:{c['id']}:{key}")] for key in LABELS]
    rows += [[Button("تعيين نص الحقوق", callback_data=f"set:{c['id']}:footer")],
             [Button("تعيين رابط القناة", callback_data=f"set:{c['id']}:channel_url")],
             [Button("عرض نص الحقوق", callback_data=f"show:{c['id']}")],
             [Button("إزالة القناة من البوت", callback_data=f"remove:{c['id']}")],
             [Button("القنوات", callback_data="channels")]]
    return Markup(rows)


async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    rows = DB.execute("SELECT id,title FROM channels ORDER BY title").fetchall()
    markup = Markup([[Button(r["title"][:60], callback_data=f"panel:{r['id']}")] for r in rows]) if rows else None
    text = "اختر قناتك لتعديل الإعدادات:" if rows else "لا توجد قنوات. أضف البوت مشرفاً ثم أرسل:\n/add @channel_username\nأو /add -1001234567890"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        if update.effective_chat.type == "private":
            await update.effective_message.reply_text(f"معرّف حسابك: {update.effective_user.id}\nلوحة الإدارة مخصّصة لصاحب البوت.")
        return
    await update.effective_message.reply_text(
        "بوت حقوق القنوات جاهز.\n"
        "1. أضفني مشرفاً بقناتك بصلاحية تعديل الرسائل.\n"
        "2. سجّلها: /add @channel_username أو /add -1001234567890\n"
        "3. افتح /channels لتعيين الحقوق والأزرار.\n"
        "الإعدادات تطبق على المنشورات الجديدة فقط.\n"
        "/id لعرض معرّفك — /cancel لإلغاء إدخال نص.")


async def user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type == "private":
        await update.effective_message.reply_text(str(update.effective_user.id))


async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    if len(context.args) != 1:
        await update.message.reply_text("استخدم /add @channel_username أو /add -1001234567890")
        return
    arg = context.args[0]
    try:
        chat = await context.bot.get_chat(int(arg) if arg.lstrip("-").isdigit() else arg)
        if chat.type != "channel":
            await update.message.reply_text("هذا ليس معرّف قناة.")
            return
        if not await authorized_channel(context.bot, chat.id):
            await update.message.reply_text("لازم يكون حساب صاحب البوت مشرفاً بالقناة أيضاً.")
            return
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
        if bot_member.status != "administrator" or not bot_member.can_edit_messages:
            await update.message.reply_text("أضف البوت مشرفاً وفعّل صلاحية تعديل الرسائل.")
            return
        c = channel(chat.id) or {"id": chat.id, "title": chat.title, **DEFAULTS}
        c["title"] = chat.title
        if not c["channel_url"] and chat.username:
            c["channel_url"] = f"https://t.me/{chat.username}"
        save_channel(c)
        await update.message.reply_text(f"تم تسجيل القناة: {chat.title}", reply_markup=settings_keyboard(c))
    except TelegramError:
        await update.message.reply_text("تعذّر الوصول للقناة. تحقق من المعرّف وصلاحيات البوت.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_owner(update):
        context.user_data.pop("pending", None)
        await update.message.reply_text("تم إلغاء الإدخال.")


async def private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    pending = context.user_data.get("pending")
    if not pending:
        return
    cid, key = pending
    c = channel(cid)
    if not c or not await authorized_channel(context.bot, cid):
        context.user_data.pop("pending", None)
        await update.message.reply_text("القناة غير متاحة أو لم تعد مشرفاً فيها.")
        return
    value = update.message.text.strip()
    if key == "footer":
        if not value or u16(value) > 512:
            await update.message.reply_text("أرسل حقوقاً غير فارغة لا تزيد عن 512 وحدة UTF-16 (الإيموجي قد يحسب وحدتين).")
            return
    elif not (value.startswith("https://t.me/") and len(value) <= 512 and not any(ch.isspace() for ch in value)):
        await update.message.reply_text("أرسل رابط تلغرام كاملاً مثل https://t.me/channel أو رابط دعوة للقناة الخاصة.")
        return
    c[key] = value
    save_channel(c)
    context.user_data.pop("pending", None)
    await update.message.reply_text("تم الحفظ. يسري على المنشورات الجديدة.", reply_markup=settings_keyboard(c))


async def panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(update):
        await q.answer("غير مسموح", show_alert=True)
        return
    await q.answer()
    data = q.data
    if data == "channels":
        context.user_data.pop("pending", None)
        await list_channels(update, context)
        return
    parts = data.split(":")
    try:
        cid = int(parts[1])
    except (ValueError, IndexError):
        return
    c = channel(cid)
    if not c or not await authorized_channel(context.bot, cid):
        await q.edit_message_text("القناة غير متاحة، أو لم تعد مشرفاً فيها. استخدم /channels")
        return
    action = parts[0]
    if action == "remove":
        # Two-click confirmation prevents accidental disabling.
        await q.edit_message_text("إزالة القناة من إدارة البوت؟ لن تُحذف أي منشورات.", reply_markup=Markup([
            [Button("تأكيد الإزالة", callback_data=f"confirmremove:{cid}")],
            [Button("رجوع", callback_data=f"panel:{cid}")]]))
        return
    if action == "confirmremove":
        with DB:
            DB.execute("DELETE FROM channels WHERE id=?", (cid,))
        await q.edit_message_text("تمت الإزالة. لإضافتها مجدداً استخدم /add")
        return
    if action == "set" and len(parts) == 3 and parts[2] in ("footer", "channel_url"):
        context.user_data["pending"] = (cid, parts[2])
        await q.edit_message_text("أرسل نص الحقوق الجديد (نص عادي)." if parts[2] == "footer" else "أرسل رابط القناة https://t.me/...\nللقناة الخاصة استخدم رابط دعوة صالحاً.")
        return
    if action == "toggle" and len(parts) == 3 and parts[2] in LABELS:
        key = parts[2]
        if key == "channel_button" and not c[key] and not c["channel_url"]:
            await q.edit_message_text("عيّن رابط القناة أولاً.", reply_markup=settings_keyboard(c))
            return
        c[key] = not c[key]
        save_channel(c)
    text = f"إعدادات القناة: {c['title']}\nالتغييرات تخص المنشورات الجديدة فقط."
    if action == "show":
        text += "\n\nنص الحقوق الحالي:\n" + c["footer"]
    try:
        await q.edit_message_text(text, reply_markup=settings_keyboard(c))
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def vote_counts(cid: int, mid: int) -> tuple[int, int]:
    rows = DB.execute("SELECT value,COUNT(*) AS n FROM votes WHERE chat=? AND msg=? GROUP BY value", (cid, mid)).fetchall()
    counts = {r["value"]: r["n"] for r in rows}
    return counts.get(1, 0), counts.get(-1, 0)


def post_keyboard(c: dict, original: list | None = None, counts=(0, 0)) -> Markup | None:
    rows = list(original or [])
    if c["channel_button"] and c["channel_url"]:
        rows.append([Button(c["title"][:60], url=c["channel_url"])])
    if c["likes"]:
        rows.append([Button(f"👍 {counts[0]}", callback_data="vote:1"), Button(f"👎 {counts[1]}", callback_data="vote:-1")])
    return Markup(rows) if rows else None


async def vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q.message or not q.from_user or q.message.chat.type != "channel":
        await q.answer("غير متاح")
        return
    cid, mid = q.message.chat.id, q.message.message_id
    row = DB.execute("SELECT keyboard FROM posts WHERE chat=? AND msg=?", (cid, mid)).fetchone()
    try:
        value = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        value = 0
    if not row or value not in (1, -1):
        await q.answer("التصويت غير متاح")
        return
    # One persisted choice per user/post; repeat click removes the choice.
    with DB:
        prev = DB.execute("SELECT value FROM votes WHERE chat=? AND msg=? AND user=?", (cid, mid, q.from_user.id)).fetchone()
        DB.execute("DELETE FROM votes WHERE chat=? AND msg=? AND user=?", (cid, mid, q.from_user.id))
        if not prev or prev["value"] != value:
            DB.execute("INSERT INTO votes VALUES (?,?,?,?)", (cid, mid, q.from_user.id, value))
    await q.answer("تم تحديث تصويتك")
    saved = json.loads(row["keyboard"])
    rows = [[Button.de_json(button, context.bot) for button in r] for r in saved]
    up, down = vote_counts(cid, mid)
    rows.append([Button(f"👍 {up}", callback_data="vote:1"), Button(f"👎 {down}", callback_data="vote:-1")])
    try:
        await q.edit_message_reply_markup(reply_markup=Markup(rows))
    except TelegramError:
        LOG.warning("Could not refresh vote keyboard for %s/%s", cid, mid)


async def apply_post(message: Message, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid, mid = message.chat_id, message.message_id
    c = channel(cid)
    if not c or not c["enabled"] or DB.execute("SELECT 1 FROM processed WHERE chat=? AND msg=?", (cid, mid)).fetchone():
        return
    if message.media_group_id and DB.execute("SELECT 1 FROM albums WHERE chat=? AND album=?", (cid, message.media_group_id)).fetchone():
        return
    is_text = message.text is not None
    supported = is_text or any((message.photo, message.video, message.animation, message.document, message.audio, message.voice))
    if not supported:
        return
    text = message.text if is_text else message.caption or ""
    entities = message.entities if is_text else message.caption_entities
    result = compose(text, entities or (), c, 4096 if is_text else 1024)
    original = [list(r) for r in message.reply_markup.inline_keyboard] if message.reply_markup else []
    markup = post_keyboard(c, original)
    # If a footer cannot fit, keep the entire original and still add enabled buttons.
    if result is None:
        final_text, final_entities = text, list(entities or ())
        if c["footer_on"] and c["footer"].strip():
            await notify(context, f"لم تتم إضافة الحقوق للمنشور {mid} في {c['title']}: لا توجد مساحة كافية. بقي المحتوى الأصلي كاملاً.")
    else:
        final_text, final_entities = result
    changed = final_text != text or list(final_entities) != list(entities or ())
    for attempt in range(3):
        try:
            if changed and is_text:
                await context.bot.edit_message_text(chat_id=cid, message_id=mid, text=final_text,
                    entities=final_entities, reply_markup=markup, link_preview_options=message.link_preview_options)
            elif changed:
                await context.bot.edit_message_caption(chat_id=cid, message_id=mid, caption=final_text,
                    caption_entities=final_entities, reply_markup=markup,
                    show_caption_above_media=message.show_caption_above_media)
            elif markup != message.reply_markup:
                await context.bot.edit_message_reply_markup(chat_id=cid, message_id=mid, reply_markup=markup)
            break
        except RetryAfter as exc:
            if attempt == 2:
                await notify(context, f"تأجلت معالجة المنشور {mid} بسبب حدود تلغرام. لم يتغير المحتوى.")
                return
            delay = exc.retry_after.total_seconds() if hasattr(exc.retry_after, "total_seconds") else float(exc.retry_after)
            await asyncio.sleep(delay + 1)
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                break
            LOG.warning("Edit rejected for %s/%s: %s", cid, mid, exc)
            await notify(context, f"تعذّر تعديل المنشور {mid} في {c['title']}. تحقق من صلاحية تعديل الرسائل ونوع المنشور.")
            return
        except Forbidden:
            await notify(context, f"البوت لا يملك صلاحية الوصول أو التعديل في {c['title']}.")
            return
    with DB:
        DB.execute("INSERT OR IGNORE INTO processed VALUES (?,?)", (cid, mid))
        if message.media_group_id:
            DB.execute("INSERT OR IGNORE INTO albums VALUES (?,?)", (cid, message.media_group_id))
        if c["likes"]:
            without_votes = markup.inline_keyboard[:-1]
            data = json.dumps([[b.to_dict() for b in r] for r in without_votes], ensure_ascii=False)
            DB.execute("INSERT OR REPLACE INTO posts VALUES (?,?,?)", (cid, mid, data))


async def flush_album(key: tuple, context: ContextTypes.DEFAULT_TYPE) -> None:
    await asyncio.sleep(1.5)
    messages = context.bot_data["album_buffer"].pop(key, [])
    if not messages:
        return
    # Prefer the caption-bearing member, otherwise first message by ID.
    chosen = min(messages, key=lambda m: (not bool(m.caption), m.message_id))
    await apply_post(chosen, context)


async def channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.channel_post  # Deliberately ignore edited_channel_post.
    if not message:
        return
    c = channel(message.chat_id)
    if not c or not c["enabled"]:
        return
    if message.media_group_id:
        key = (message.chat_id, message.media_group_id)
        if DB.execute("SELECT 1 FROM albums WHERE chat=? AND album=?", key).fetchone():
            return
        buffer = context.bot_data.setdefault("album_buffer", {})
        if key not in buffer:
            buffer[key] = []
            buffer[key].append(message)
            context.application.create_task(flush_album(key, context), update=update)
        else:
            buffer[key].append(message)
    else:
        await apply_post(message, context)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Do not log whole updates, request URLs or tracebacks containing bot tokens.
    LOG.error("Unhandled error type: %s", type(context.error).__name__)
    await notify(context, "حدث خطأ أثناء معالجة تحديث. راجع سجل التشغيل؛ قد يبقى المنشور المعني بلا حقوق.")


def main() -> None:
    global OWNER_ID
    load_env()
    token = os.getenv("BOT_TOKEN", "")
    owner = os.getenv("OWNER_ID", "")
    if not token or "YOUR_" in token or not owner.isdigit() or int(owner) <= 0 or "YOUR_" in owner:
        raise SystemExit("ضع BOT_TOKEN وOWNER_ID الصحيحين في ملف .env أولاً.")
    OWNER_ID = int(owner)
    init_db(BASE / "bot.sqlite3")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    app = Application.builder().token(token).concurrent_updates(False).build()
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", start, filters=private))
    app.add_handler(CommandHandler("id", user_id, filters=private))
    app.add_handler(CommandHandler("channels", list_channels, filters=private))
    app.add_handler(CommandHandler("add", add_channel, filters=private))
    app.add_handler(CommandHandler("cancel", cancel, filters=private))
    app.add_handler(CallbackQueryHandler(vote_callback, pattern=r"^vote:"))
    app.add_handler(CallbackQueryHandler(panel_callback, pattern=r"^(channels$|panel:|toggle:|set:|show:|remove:|confirmremove:)"))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, channel_post))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, private_text))
    app.add_error_handler(error_handler)
    LOG.info("Starting one polling worker")
    try:
        app.run_polling(allowed_updates=["message", "channel_post", "callback_query"],
                        drop_pending_updates=False, bootstrap_retries=3)
    finally:
        DB.close()


if __name__ == "__main__":
    main()
