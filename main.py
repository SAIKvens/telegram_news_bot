import os
import asyncio
import sqlite3
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, CallbackQuery, Update
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from dotenv import load_dotenv
from pathlib import Path
import openai

# Load environment variables
dotenv_path = Path("/Users/saicvensor/Documents/telegram_news_bot/.env")
load_dotenv(dotenv_path=dotenv_path)

BOT_TOKEN = os.getenv("BOT_TOKEN") or "7921989032:AAGJAJGZv8OIb3-VeuR1nETq1vXt8EJvxLY"
CHANNEL_ID = os.getenv("CHANNEL_ID") or "@moneygrit"
ADMINS = os.getenv("ADMINS", "7640784079").split(",")
SIGNATURE = '[Деньги с Характером](https://t.me/moneygrit)'
DB_PATH = "posts.db"

app = FastAPI()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

# --- DB ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                status TEXT CHECK(status IN ('sent', 'scheduled')) NOT NULL,
                scheduled_at TEXT,
                message_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()

# --- GPT ---
async def rewrite_with_gpt(text: str, style_prompt: str) -> str:
    try:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return "[GPT Error]: OPENAI_API_KEY не найден."
        client = openai.AsyncOpenAI(api_key=api_key)
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": f"Ты переписываешь текст в стиле: {style_prompt}"},
                {"role": "user", "content": text}
            ]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"[GPT Error]: {e}"

# --- Bot Commands ---
async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Начать работу"),
        BotCommand(command="newpost", description="Новый пост"),
        BotCommand(command="editpost", description="Редактировать пост"),
        BotCommand(command="showallposts", description="Показать все посты"),
    ]
    await bot.set_my_commands(commands)

# --- Utility ---
def is_admin(msg):
    return str(msg.from_user.id) in ADMINS

# --- Webhook startup ---
@app.on_event("startup")
async def on_startup():
    init_db()
    await set_bot_commands(bot)
    dp.include_router(router)
    scheduler.configure(event_loop=asyncio.get_running_loop())
    scheduler.start()
    webhook_url = os.getenv("WEBHOOK_HOST", "") + "/webhook"
    await bot.set_webhook(webhook_url)

@app.on_event("shutdown")
async def on_shutdown():
    await bot.delete_webhook()

@app.post("/webhook")
async def handle_webhook(request: Request):
    body = await request.json()
    update = Update(**body)
    await dp.feed_update(bot, update)
    return {"ok": True}

# --- POST SENDING LOGIC ---
async def send_to_channel(text):
    if SIGNATURE in text:
        full_post = text
    else:
        full_post = f"{text}\n\n{SIGNATURE}"
    msg = await bot.send_message(CHANNEL_ID, full_post, parse_mode="Markdown", disable_web_page_preview=True)
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("UPDATE posts SET status='sent', message_id=? WHERE text=? AND status='scheduled'", (msg.message_id, text))
        conn.commit()

# --- /start and /newpost ---
@router.message(F.text.in_(['/start', '/newpost']))
async def cmd_start_newpost(msg: Message, state: FSMContext):
    if not is_admin(msg):
        return
    await state.clear()
    await msg.answer("Пришли текст поста")

    # Обнуляем FSM, далее ждем любой текст (без состояния)

# --- POST TEXT ENTRY ---
@router.message(lambda msg: is_admin(msg) and not msg.text.startswith('/'))
async def handle_post_text(msg: Message, state: FSMContext):
    if not msg.text or msg.text.startswith("/"):
        return
    markup = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Оставить как есть")],
        [KeyboardButton(text="Переписать с помощью нейросети")]
    ], resize_keyboard=True, one_time_keyboard=True)
    await state.set_data({"original_text": msg.text.strip()})
    await state.set_state("rewrite_choice")
    await msg.answer("Использовать нейросеть для переписывания или оставить как есть?", reply_markup=markup)

# --- REWRITE CHOICE ---
@router.message(F.text.in_(["Оставить как есть", "Переписать с помощью нейросети"]))
async def handle_rewrite_choice(msg: Message, state: FSMContext):
    data = await state.get_data()
    original_text = data.get("original_text", "")
    if msg.text == "Оставить как есть":
        post_text = original_text
    else:
        style_prompt = "Пиши в стиле опытного бизнесмена. Кратко, уверенно, с конкретикой. Без воды, без смайликов. Максимум пользы. Пост — как выжимка инсайтов для подписчиков канала «Деньги с Характером»."
        post_text = await rewrite_with_gpt(original_text, style_prompt)
        await msg.answer("Вот переписанный пост:\n\n" + post_text)
    await state.set_data({"post_text": post_text})
    markup = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Отправить сейчас")],
        [KeyboardButton(text="Запланировать")]
    ], resize_keyboard=True, one_time_keyboard=True)
    await msg.answer("Что сделать с постом?", reply_markup=markup)
    await state.set_state("post_action")

# --- ACTION CHOICE ---
@router.message(F.text.in_(["Отправить сейчас", "Запланировать"]))
async def handle_action_choice(msg: Message, state: FSMContext):
    data = await state.get_data()
    post_text = data.get("post_text", "")
    if SIGNATURE in post_text:
        full_post = post_text
    else:
        full_post = f"{post_text}\n\n{SIGNATURE}"
    if msg.text == "Отправить сейчас":
        sent = await bot.send_message(CHANNEL_ID, full_post, parse_mode="Markdown", disable_web_page_preview=True)
        await msg.answer("Пост отправлен ✅", reply_markup=ReplyKeyboardRemove())
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO posts (text, status, message_id) VALUES (?, ?, ?)", (full_post, "sent", sent.message_id))
            conn.commit()
        await state.clear()
    else:
        await msg.answer("Во сколько запланировать? (в формате HH:MM)", reply_markup=ReplyKeyboardRemove())
        await state.set_state("schedule_time")

# --- SCHEDULE TIME ---
@router.message(lambda msg: ":" in msg.text and len(msg.text.strip()) <= 5)
async def handle_schedule_time(msg: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state != "schedule_time":
        return
    data = await state.get_data()
    post_text = data.get("post_text", "")
    try:
        hour, minute = map(int, msg.text.strip().split(":"))
        now = datetime.now()
        publish_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if publish_time <= now:
            publish_time += timedelta(days=1)
        scheduler.add_job(send_to_channel, trigger=DateTrigger(run_date=publish_time), args=[post_text])
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO posts (text, status, scheduled_at) VALUES (?, ?, ?)", (post_text, "scheduled", publish_time.strftime("%Y-%m-%d %H:%M")))
            conn.commit()
        await msg.answer(f"Пост запланирован на {publish_time.strftime('%Y-%m-%d %H:%M')} ✅")
        await state.clear()
    except Exception:
        await msg.answer("Неверный формат времени. Используйте HH:MM")

# --- /editpost ---
@router.message(F.text == "/editpost")
async def handle_editpost(msg: Message, state: FSMContext):
    if not is_admin(msg):
        return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отправленные", callback_data="edit_sent")],
        [InlineKeyboardButton(text="Запланированные", callback_data="edit_scheduled")]
    ])
    await msg.answer("Выбери тип постов для редактирования:", reply_markup=kb)

@router.callback_query(F.data.in_(["edit_sent", "edit_scheduled"]))
async def handle_pick_post_type(callback: CallbackQuery, state: FSMContext):
    post_type = "sent" if callback.data == "edit_sent" else "scheduled"
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        if post_type == "sent":
            c.execute("SELECT id, text FROM posts WHERE status='sent' ORDER BY id DESC LIMIT 10")
        else:
            c.execute("SELECT id, text, scheduled_at FROM posts WHERE status='scheduled' ORDER BY id DESC LIMIT 10")
        posts = c.fetchall()
    if not posts:
        await callback.message.answer("Нет постов для редактирования.")
        await callback.answer()
        return
    buttons = []
    for p in posts:
        label = p[1][:30].replace('\n', ' ') + ("..." if len(p[1]) > 30 else "")
        label += f" [{p[2]}]" if post_type == "scheduled" else ""
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"editpost_{post_type}_{p[0]}")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.answer("Выбери пост для редактирования:", reply_markup=kb)
    await callback.answer()

@router.callback_query(lambda c: c.data.startswith("editpost_"))
async def handle_editpost_pick(callback: CallbackQuery, state: FSMContext):
    _, post_type, post_id = callback.data.split("_")
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT text FROM posts WHERE id=?", (post_id,))
        row = c.fetchone()
    if not row:
        await callback.message.answer("Пост не найден.")
        await callback.answer()
        return
    await state.set_data({"edit_post_id": post_id, "edit_post_type": post_type, "edit_post_text": row[0]})
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Редактировать вручную")],
        [KeyboardButton(text="Переписать с GPT")]
    ], resize_keyboard=True, one_time_keyboard=True)
    await callback.message.answer(f"Текущий текст:\n\n{row[0]}\n\nВыбери способ редактирования:", reply_markup=kb)
    await state.set_state("edit_choice")
    await callback.answer()

@router.message(F.text.in_(["Редактировать вручную", "Переписать с GPT"]))
async def handle_edit_choice(msg: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state != "edit_choice":
        return
    data = await state.get_data()
    if msg.text == "Редактировать вручную":
        await msg.answer("Введи новый текст поста:", reply_markup=ReplyKeyboardRemove())
        await state.set_state("edit_manual")
    else:
        style_prompt = "Пиши в стиле опытного бизнесмена. Кратко, уверенно, с конкретикой. Без воды, без смайликов. Максимум пользы. Пост — как выжимка инсайтов для подписчиков канала «Деньги с Характером»."
        new_text = await rewrite_with_gpt(data["edit_post_text"], style_prompt)
        await state.set_data({"edit_new_text": new_text})
        await msg.answer("Вот вариант:\n\n" + new_text)
        kb = ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="Сохранить")],
            [KeyboardButton(text="Отмена")]
        ], resize_keyboard=True, one_time_keyboard=True)
        await msg.answer("Сохранить этот вариант?", reply_markup=kb)
        await state.set_state("edit_confirm")

@router.message()
async def handle_edit_manual(msg: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state != "edit_manual":
        return
    await state.set_data({"edit_new_text": msg.text.strip()})
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Сохранить")],
        [KeyboardButton(text="Отмена")]
    ], resize_keyboard=True, one_time_keyboard=True)
    await msg.answer("Сохранить этот вариант?", reply_markup=kb)
    await state.set_state("edit_confirm")

@router.message(F.text == "Сохранить")
async def handle_edit_save(msg: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state != "edit_confirm":
        return
    data = await state.get_data()
    post_id = data.get("edit_post_id")
    new_text = data.get("edit_new_text")
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("UPDATE posts SET text=? WHERE id=?", (new_text, post_id))
        conn.commit()
    await msg.answer("Пост обновлен ✅", reply_markup=ReplyKeyboardRemove())
    await state.clear()

@router.message(F.text == "Отмена")
async def handle_edit_cancel(msg: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state != "edit_confirm":
        return
    await msg.answer("Редактирование отменено", reply_markup=ReplyKeyboardRemove())
    await state.clear()

# --- /showallposts ---
@router.message(F.text == "/showallposts")
async def handle_showallposts(msg: Message, state: FSMContext):
    if not is_admin(msg):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отправленные", callback_data="show_sent")],
        [InlineKeyboardButton(text="Запланированные", callback_data="show_scheduled")]
    ])
    await msg.answer("Выбери категорию:", reply_markup=kb)
    await state.clear()

@router.callback_query(F.data.in_(["show_sent", "show_scheduled"]))
async def handle_show_category(callback: CallbackQuery, state: FSMContext):
    cat = "sent" if callback.data == "show_sent" else "scheduled"
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        if cat == "sent":
            c.execute("SELECT id, text, created_at FROM posts WHERE status='sent' ORDER BY id DESC LIMIT 10")
        else:
            c.execute("SELECT id, text, scheduled_at FROM posts WHERE status='scheduled' ORDER BY id DESC LIMIT 10")
        posts = c.fetchall()
    if not posts:
        await callback.message.answer("Нет постов.")
        await callback.answer()
        return
    for p in posts:
        text = p[1]
        time_info = p[2]
        msg_text = f"{text}\n\n⏰ {time_info}" if cat == "scheduled" else text
        await callback.message.answer(msg_text, parse_mode="Markdown", disable_web_page_preview=True)
    await callback.answer()
