import os
import sqlite3
import random
from datetime import datetime, timedelta, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
CHANNEL_ID = os.environ["CHANNEL_ID"]
DB_PATH = os.environ.get("DB_PATH", "contests.db")
CREATE_PRIZE, CREATE_HOURS = range(2)
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS contests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prize TEXT NOT NULL,
            ends_at TEXT NOT NULL,
            channel_message_id INTEGER,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS participants (
            contest_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            first_name TEXT,
            UNIQUE(contest_id, user_id)
        )
    """)
    con.commit()
    return con
def is_admin(update: Update) -> bool:
    return (
        update.effective_user is not None
        and update.effective_user.id == ADMIN_ID
    )
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text(
            "Бот готов. Участие в конкурсах происходит через кнопку "
            "в посте канала."
        )
        return
    keyboard = [
        [
            InlineKeyboardButton(
                "🎁 Создать конкурс",
                callback_data="create",
            )
        ],
        [
            InlineKeyboardButton(
                "📋 Активные конкурсы",
                callback_data="active",
            )
        ],
    ]
    await update.message.reply_text(
        "Панель управления конкурсами:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    if query.data == "create":
        await query.message.reply_text(
            "Напиши приз конкурса.\n\n"
            "Например: 10 000 ₽"
        )
        return CREATE_PRIZE
    if query.data == "active":
        con = db()
        rows = con.execute(
            "SELECT * FROM contests "
            "WHERE active=1 "
            "ORDER BY id DESC"
        ).fetchall()
        con.close()
        if not rows:
            await query.message.reply_text(
                "Активных конкурсов нет."
            )
        else:
            text = "\n".join(
                f"#{row['id']} — {row['prize']} — "
                f"до {row['ends_at']}"
                for row in rows
            )
            await query.message.reply_text(text)
async def prize_received(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    context.user_data["prize"] = update.message.text.strip()
    await update.message.reply_text(
        "На сколько часов запустить конкурс?\n\n"
        "Например: 24"
    )
    return CREATE_HOURS
async def hours_received(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    try:
        hours = float(
            update.message.text.replace(",", ".")
        )
        if hours <= 0 or hours > 720:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Укажи число часов от 0.1 до 720."
        )
        return CREATE_HOURS
    prize = context.user_data.pop("prize")
    ends = datetime.now(timezone.utc) + timedelta(
        hours=hours
    )
    con = db()
    cursor = con.execute(
        """
        INSERT INTO contests (prize, ends_at)
        VALUES (?, ?)
        """,
        (
            prize,
            ends.isoformat(),
        ),
    )
    contest_id = cursor.lastrowid
    con.commit()
    con.close()
    text = (
        "🎁 КОНКУРС!\n\n"
        f"Приз: {prize}\n\n"
        "Чтобы участвовать:\n"
        "1. Подпишись на канал.\n"
        "2. Нажми кнопку «Участвовать».\n\n"
        f"⏰ Итоги: "
        f"{ends.astimezone().strftime('%d.%m.%Y %H:%M')}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🎉 Участвовать",
                    callback_data=f"join:{contest_id}",
                )
            ]
        ]
    )
    message = await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        reply_markup=keyboard,
    )
    con = db()
    con.execute(
        """
        UPDATE contests
        SET channel_message_id=?
        WHERE id=?
        """,
        (
            message.message_id,
            contest_id,
        ),
    )
    con.commit()
    con.close()
    context.job_queue.run_once(
        finish_contest,
        when=timedelta(hours=hours),
        data=contest_id,
        name=f"contest:{contest_id}",
    )
    await update.message.reply_text(
        f"Конкурс #{contest_id} опубликован в канале."
    )
    return ConversationHandler.END
async def join(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    try:
        contest_id = int(
            query.data.split(":")[1]
        )
    except Exception:
        await query.answer()
        return
    con = db()
    contest = con.execute(
        """
        SELECT *
        FROM contests
        WHERE id=? AND active=1
        """,
        (contest_id,),
    ).fetchone()
    con.close()
    if not contest:
        await query.answer(
            "Этот конкурс уже завершён.",
            show_alert=True,
        )
        return
    try:
        member = await context.bot.get_chat_member(
            CHANNEL_ID,
            query.from_user.id,
        )
        allowed = member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        }
    except Exception:
        allowed = False
    if not allowed:
        await query.answer(
            "Сначала подпишись на канал, "
            "затем нажми кнопку ещё раз.",
            show_alert=True,
        )
        return
    con = db()
    con.execute(
        """
        INSERT OR IGNORE INTO participants
        (contest_id, user_id, username, first_name)
        VALUES (?, ?, ?, ?)
        """,
        (
            contest_id,
            query.from_user.id,
            query.from_user.username,
            query.from_user.first_name,
        ),
    )
    con.commit()
    con.close()
    await query.answer(
        "Ты участвуешь! Удачи 🎉",
        show_alert=True,
    )
async def finish_contest(
    context: ContextTypes.DEFAULT_TYPE,
):
    contest_id = context.job.data
    con = db()
    contest = con.execute(
        "SELECT * FROM contests WHERE id=?",
        (contest_id,),
    ).fetchone()
    if not contest or not contest["active"]:
        con.close()
        return
    participants = con.execute(
        """
        SELECT *
        FROM participants
        WHERE contest_id=?
        """,
        (contest_id,),
    ).fetchall()
    con.execute(
        """
        UPDATE contests
        SET active=0
        WHERE id=?
        """,
        (contest_id,),
    )
    con.commit()
    con.close()
    if not participants:
        result = (
            f"🏁 Конкурс #{contest_id} завершён.\n\n"
            "Участников не было."
        )
    else:
        winner = random.choice(participants)
        if winner["username"]:
            mention = f"@{winner['username']}"
        else:
            mention = winner["first_name"]
        result = (
            f"🏆 КОНКУРС #{contest_id} ЗАВЕРШЁН!\n\n"
            f"Приз: {contest['prize']}\n"
            f"Победитель: {mention} 🎉"
        )
    try:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=result,
        )
    except Exception:
        pass
async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    context.user_data.clear()
    await update.message.reply_text(
        "Создание конкурса отменено."
    )
    return ConversationHandler.END
def main():
    db()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )
    conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                menu_callback,
                pattern="^create$",
            )
        ],
        states={
            CREATE_PRIZE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    prize_received,
                )
            ],
            CREATE_HOURS: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    hours_received,
                )
            ],
        },
        fallbacks=[
            CommandHandler(
                "cancel",
                cancel,
            )
        ],
        per_user=True,
        per_chat=True,
    )
    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )
    app.add_handler(conversation)
    app.add_handler(
        CallbackQueryHandler(
            menu_callback,
            pattern="^(active|create)$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            join,
            pattern=r"^join:\d+$",
        )
    )
    app.run_polling()
if __name__ == "__main__":
    main()
