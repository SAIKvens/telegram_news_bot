from fastapi import FastAPI, Request
import uvicorn
from aiogram.types import Update
import os
import re
import asyncio
import openai
import sqlite3

DB_PATH = "posts.db"

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

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.types import BotCommand
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pathlib import Path
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

# Загружаем переменные окружения
dotenv_path = Path("/Users/saicvensor/Documents/telegram_news_bot/.env")
load_dotenv(dotenv_path=dotenv_path)

# Константы
BOT_TOKEN = "7921989032:AAGJAJGZv8OIb3-VeuR1nETq1vXt8EJvxLY"
CHANNEL_ID = "@moneygrit"
ADMINS = ["7640784079"]
SIGNATURE = '[Деньги с Характером](https://t.me/moneygrit)'

sent_posts = []
scheduled_posts = []

router = Router()

async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Начать работу"),
        BotCommand(command="new_post", description="Новый пост"),
        BotCommand(command="edit_post", description="Редактировать пост"),
        BotCommand(command="show_posts", description="Все посты"),
        BotCommand(command="delete_all", description="Удалить все посты"),
    ]
    await bot.set_my_commands(commands)

class PostStates(StatesGroup):
    waiting_for_post = State()
    waiting_for_rewrite_choice = State()
    waiting_for_action = State()
    waiting_for_time = State()

@router.message(F.text == "/start")
async def start_command(msg: Message, state: FSMContext):
    await msg.answer("Привет! Присылай посты.")
    await handle_show_posts(msg)
    await state.set_state(PostStates.waiting_for_post)

@router.message(F.text == "/new_post")
async def handle_new_post(msg: Message, state: FSMContext):
    await state.clear()
    await state.set_state(PostStates.waiting_for_post)
    await msg.answer("Пришли текст поста")

@router.callback_query(F.data == "edit_post")
async def handle_edit_post(callback):
    await callback.message.answer("Пока функция редактирования не реализована")
    await callback.answer()

@router.message(F.text == "/show_posts")
async def handle_show_posts(msg: Message):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT text FROM posts WHERE status = 'sent' ORDER BY id DESC LIMIT 10")
        sent = c.fetchall()
        c.execute("SELECT text, scheduled_at FROM posts WHERE status = 'scheduled' ORDER BY id DESC LIMIT 10")
        scheduled = c.fetchall()

    if not sent and not scheduled:
        await msg.answer("Пока список постов пуст")
        return

    if sent:
        await msg.answer("✅ Отправленные посты:")
        for row in sent:
            await msg.answer(row[0], parse_mode="Markdown", disable_web_page_preview=True)

    if scheduled:
        await msg.answer("⏳ Отложенные посты:")
        for row in scheduled:
            await msg.answer(f"{row[0]}\n\n⏰ Запланировано на: {row[1]}", parse_mode="Markdown", disable_web_page_preview=True)

@router.callback_query(F.data == "delete_all")
async def handle_delete_all(callback):
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Да", callback_data="delete_all_confirm"),
            InlineKeyboardButton(text="Нет", callback_data="delete_all_cancel")
        ]
    ])
    await callback.message.answer("❗️Ты точно хочешь удалить все запланированные и отправленные посты?", reply_markup=confirm_kb)
    await callback.answer()

# Базовая настройка
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

# FastAPI app
app = FastAPI()

def extract_time_and_text(msg: str):
    pattern = r"^(?:[01]\d|2[0-3]):[0-5]\d\s*\n(.+)$"
    match = re.match(pattern, msg, re.DOTALL)
    if not match:
        return None, None, None
    time_part = msg.split('\n', 1)[0]
    text_part = match.group(1).strip()
    hour, minute = map(int, time_part.split(":"))
    return hour, minute, text_part

async def send_to_channel(text):
    if SIGNATURE in text:
        full_post = text
    else:
        full_post = f"{text}\n\n{SIGNATURE}"
    await bot.send_message(CHANNEL_ID, full_post, parse_mode="Markdown", disable_web_page_preview=True)

@router.message(PostStates.waiting_for_post, F.from_user.id.in_(map(int, ADMINS)))
async def handle_post(msg: Message, state: FSMContext):
    if not msg.text or msg.text.startswith("/"):
        return

    await state.update_data(original_text=msg.text.strip())
    markup = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Оставить как есть")],
        [KeyboardButton(text="Переписать с помощью нейросети")]
    ], resize_keyboard=True, one_time_keyboard=True)
    await msg.answer("Готово! Использовать нейросеть для переписывания или оставить как есть?", reply_markup=markup)
    await state.set_state(PostStates.waiting_for_rewrite_choice)


# Новый хендлер для выбора оставить как есть или переписать
@router.message(PostStates.waiting_for_rewrite_choice, F.text.in_(["Оставить как есть", "Переписать с помощью нейросети"]))
async def handle_rewrite_choice(msg: Message, state: FSMContext):
    data = await state.get_data()
    original_text = data.get("original_text", "")

    if msg.text == "Оставить как есть":
        await state.update_data(post_text=original_text)
    else:
        style_prompt = "Пиши в стиле опытного бизнесмена. Кратко, уверенно, с конкретикой. Без воды, без смайликов. Максимум пользы. Пост — как выжимка инсайтов для подписчиков канала «Деньги с Характером»."
        rewritten = await rewrite_with_gpt(original_text, style_prompt)
        await state.update_data(post_text=rewritten)
        await msg.answer("Вот переписанный пост:\n\n" + rewritten)

    markup = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Отправить сейчас")],
        [KeyboardButton(text="Запланировать")]
    ], resize_keyboard=True, one_time_keyboard=True)
    await msg.answer("Что сделать с постом?", reply_markup=markup)
    await state.set_state(PostStates.waiting_for_action)

async def rewrite_with_gpt(text: str, style_prompt: str) -> str:
    try:
        api_key = os.getenv("OPENAI_API_KEY")
        print("DEBUG API KEY:", api_key)

        if not api_key:
            return "[GPT Error]: OPENAI_API_KEY не найден. Убедись, что .env загружен корректно."

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

@router.message(PostStates.waiting_for_action, F.text.in_(["Отправить сейчас", "Запланировать"]))
async def handle_action(msg: Message, state: FSMContext):
    data = await state.get_data()
    post_text = data.get("post_text", "")
    if SIGNATURE in post_text:
        full_post = post_text
    else:
        full_post = f"{post_text}\n\n{SIGNATURE}"

    if msg.text == "Отправить сейчас":
        await bot.send_message(CHANNEL_ID, full_post, parse_mode="Markdown", disable_web_page_preview=True)
        await msg.answer("Пост отправлен ✅", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        sent_posts.append(full_post)
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO posts (text, status) VALUES (?, ?)", (full_post, "sent"))
            conn.commit()
    else:
        await msg.answer("Во сколько запланировать? (в формате HH:MM)", reply_markup=ReplyKeyboardRemove())
        await state.set_state(PostStates.waiting_for_time)

@router.message(PostStates.waiting_for_time)
async def handle_time(msg: Message, state: FSMContext):
    data = await state.get_data()
    post_text = data.get("post_text", "")
    if SIGNATURE in post_text:
        full_post = post_text
    else:
        full_post = f"{post_text}\n\n{SIGNATURE}"

    try:
        hour, minute = map(int, msg.text.strip().split(":"))
        now = datetime.now()
        publish_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if publish_time <= now:
            publish_time += timedelta(days=1)

        trigger = DateTrigger(run_date=publish_time)
        scheduler.add_job(send_to_channel, trigger=trigger, args=[full_post])
        scheduled_posts.append(full_post)

        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO posts (text, status, scheduled_at) VALUES (?, ?, ?)", (full_post, "scheduled", publish_time.strftime("%Y-%m-%d %H:%M")))
            conn.commit()

        await msg.answer(f"Пост запланирован на {publish_time.strftime('%Y-%m-%d %H:%M')} ✅")
        await state.clear()
    except:
        await msg.answer("Неверный формат времени. Используйте HH:MM")



# FastAPI startup/shutdown and webhook
@app.on_event("startup")
async def on_startup():
    init_db()
    await set_bot_commands(bot)
    dp.include_router(router)
    scheduler.configure(event_loop=asyncio.get_running_loop())
    scheduler.start()
    webhook_url = os.getenv("WEBHOOK_HOST") + "/webhook"
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
