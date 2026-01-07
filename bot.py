import os
import logging
import datetime
import aiosqlite
import replicate
import asyncio

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.bot import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
from deep_translator import GoogleTranslator
from aiogram.types import FSInputFile

# ================== CONFIG ==================

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
REPLICATE_TOKEN = os.getenv("REPLICATE_API_TOKEN", "").strip()

if not TELEGRAM_TOKEN or not REPLICATE_TOKEN:
    raise RuntimeError("❌ Нет TELEGRAM_BOT_TOKEN или REPLICATE_API_TOKEN")

os.environ["REPLICATE_API_TOKEN"] = REPLICATE_TOKEN

DB_NAME = "telegram_users.db"
FREE_DAILY_LIMIT = 1

logging.basicConfig(level=logging.INFO)

# ================== BOT ==================

bot = Bot(
    token=TELEGRAM_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher(storage=MemoryStorage())
router = Router()

user_states = {}

# ================== DB ==================

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            created_at TEXT,
            generation_tokens INTEGER DEFAULT 0
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS daily_usage (
            telegram_id INTEGER,
            date TEXT,
            used INTEGER DEFAULT 0,
            PRIMARY KEY (telegram_id, date)
        )
        """)
        await db.commit()

async def register_user(telegram_id: int, username: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
        INSERT OR IGNORE INTO users
        (telegram_id, username, created_at)
        VALUES (?, ?, ?)
        """, (telegram_id, username, datetime.datetime.utcnow().date().isoformat()))
        await db.commit()

async def get_balance(telegram_id: int) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT generation_tokens FROM users WHERE telegram_id = ?",
            (telegram_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else 0

# ================== TOKENS ==================

async def use_free_generation(telegram_id: int) -> bool:
    today = datetime.datetime.utcnow().date().isoformat()

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("BEGIN IMMEDIATE")

        cur = await db.execute("""
        SELECT used FROM daily_usage
        WHERE telegram_id = ? AND date = ?
        """, (telegram_id, today))
        row = await cur.fetchone()

        if row is None:
            await db.execute("""
            INSERT INTO daily_usage (telegram_id, date, used)
            VALUES (?, ?, 1)
            """, (telegram_id, today))
            await db.commit()
            return True

        if row[0] < FREE_DAILY_LIMIT:
            await db.execute("""
            UPDATE daily_usage
            SET used = used + 1
            WHERE telegram_id = ? AND date = ?
            """, (telegram_id, today))
            await db.commit()
            return True

        await db.rollback()
        return False

async def can_generate(telegram_id: int) -> str | None:
    if await use_free_generation(telegram_id):
        return "free"

    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("""
        SELECT generation_tokens FROM users WHERE telegram_id = ?
        """, (telegram_id,))
        row = await cur.fetchone()
        if row and row[0] > 0:
            return "paid"

    return None

async def finalize_generation(telegram_id: int, gen_type: str):
    if gen_type == "paid":
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("""
            UPDATE users
            SET generation_tokens = generation_tokens - 1
            WHERE telegram_id = ?
            """, (telegram_id,))
            await db.commit()

# ================== UTILS ==================

def translate(text: str) -> str:
    try:
        return GoogleTranslator(source="auto", target="en").translate(text)
    except:
        return text

# ================== HELPERS ==================

async def show_ratio_selection(message: Message):
    """Показывает кнопки выбора соотношения сторон"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1:1 (квадрат)", callback_data="set_ratio:1:1")],
        [InlineKeyboardButton(text="16:9 (широкоформатный)", callback_data="set_ratio:16:9")],
        [InlineKeyboardButton(text="9:16 (вертикальный)", callback_data="set_ratio:9:16")],
        [InlineKeyboardButton(text="4:3", callback_data="set_ratio:4:3")],
        [InlineKeyboardButton(text="3:2", callback_data="set_ratio:3:2")],
        [InlineKeyboardButton(text="↩️ Назад к режимам", callback_data="back_to_modes")]
    ])
    await message.answer("📏 Выберите соотношение сторон:", reply_markup=kb)
    
async def show_ratio_selection_img2img(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1:1 (квадрат)", callback_data="set_ratio_img2img:1:1")],
        [InlineKeyboardButton(text="16:9 (широкоформатный)", callback_data="set_ratio_img2img:16:9")],
        [InlineKeyboardButton(text="9:16 (вертикальный)", callback_data="set_ratio_img2img:9:16")],
        [InlineKeyboardButton(text="4:3", callback_data="set_ratio_img2img:4:3")],
        [InlineKeyboardButton(text="3:2", callback_data="set_ratio_img2img:3:2")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_modes")]
    ])
    await message.answer("📏 Выберите соотношение сторон для результата:", reply_markup=kb)
    
@router.callback_query(F.data.startswith("set_ratio_img2img:"))
async def set_ratio_img2img(callback: CallbackQuery):
    user_id = callback.from_user.id
    ratio = callback.data.split(":", 1)[1]

    if user_id not in user_states:
        user_states[user_id] = {}

    user_states[user_id]["mode"] = "img2img"
    user_states[user_id]["aspect_ratio"] = ratio
    user_states[user_id]["images"] = []

    await callback.message.answer(
        f"✅ Соотношение сторон: <b>{ratio}</b>\n\n"
        "📸 Теперь отправьте изображение"
    )
    await callback.answer()

async def show_main_menu(message_or_callback):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖼️ Создать картинку с нуля", callback_data="select_mode:txt2img")],
        [InlineKeyboardButton(text="📷 Редактировать ваше фото", callback_data="select_mode:img2img")],
        [InlineKeyboardButton(text="💰 Получить токены", callback_data="banans:banans")]
    ])

    photo = FSInputFile("img/banana3.png")
    caption = (
        "🎨 <b>AI Image Generator</b>\n\n"
        "Выберите режим генерации 👇"
    )

    if isinstance(message_or_callback, Message):
        await message_or_callback.answer_photo(
            photo=photo,
            caption=caption,
            reply_markup=kb
        )
    else:
        await message_or_callback.message.answer_photo(
            photo=photo,
            caption=caption,
            reply_markup=kb
        )

@router.message(Command("start"))
async def start(message: Message):
    await register_user(message.from_user.id, message.from_user.username)
    await show_main_menu(message)

@router.message(Command("menu"))
async def menu(message: Message):
    await show_main_menu(message)

@router.callback_query(F.data == "back_to_modes")
async def back_to_modes(callback: CallbackQuery):
    user_states.pop(callback.from_user.id, None)
    await show_main_menu(callback)
    await callback.answer()

@router.message(Command("txt2img"))
async def txt2img(message: Message):
    user_id = message.from_user.id
    user_states[user_id] = {"mode": "txt2img"}

    await message.answer("📏 Выберите соотношение сторон:")
    await show_ratio_selection(message)



@router.callback_query(F.data.startswith("select_mode:"))
async def handle_mode_selection(callback: CallbackQuery):
    user_id = callback.from_user.id
    mode = callback.data.split(":", 1)[1]

    user_states.setdefault(user_id, {})

    if mode == "txt2img":
        await callback.message.answer("📏 Выберите соотношение сторон:")
        await show_ratio_selection(callback.message)

    elif mode == "img2img":
        user_states[user_id]["mode"] = "img2img"
        await show_ratio_selection_img2img(callback.message)

    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("set_ratio:"))
async def set_ratio(callback: CallbackQuery):
    user_id = callback.from_user.id
    ratio = callback.data.split(":", 1)[1]

    if user_id not in user_states:
        user_states[user_id] = {}
    
    user_states[user_id]["aspect_ratio"] = ratio
    user_states[user_id]["mode"] = "txt2img"  # Убедимся, что режим установлен

    await callback.answer("✅ Выбрано!")
    await callback.message.edit_text(
        f"✅ Соотношение сторон: <b>{ratio}</b>\n\n")

@router.message(Command("ratio"))
async def cmd_ratio(message: Message):
    await show_ratio_selection(message)



async def show_balance(message_or_callback, user_id: int):
    banans = await get_balance(user_id)
    user = message_or_callback.from_user if isinstance(message_or_callback, Message) else message_or_callback.from_user

    photo = FSInputFile("img/banana3.png")
    base_url2 = "https://t.me/tribute/app?startapp=ppf9"
    base_url5 = "https://t.me/tribute/app?startapp=ppgM"
    base_url10 = "https://t.me/tribute/app?startapp=ppgN"
    base_url30 = "https://t.me/tribute/app?startapp=ppgO"
    base_url80 = "https://t.me/tribute/app?startapp=ppha"
    base_url150 = "https://t.me/tribute/app?startapp=ppgQ"
    base_url200 = "https://t.me/tribute/app?startapp=ppgS"
   

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍌 2 генерации — 110 ₽", url=base_url2)],
        [InlineKeyboardButton(text="🍌 5 генераций — 260 ₽", url=base_url5)],
        [InlineKeyboardButton(text="🍌 10 генераций — 490 ₽", url=base_url10)],
        [InlineKeyboardButton(text="⭐ 30 генераций — 1 350 ₽", url=base_url30)],
        [InlineKeyboardButton(text="🍌 80 генераций — 3 600 ₽ ₽", url=base_url80)],
        [InlineKeyboardButton(text="🍌 150 генераций — 5 700 ₽", url=base_url150)],
        [InlineKeyboardButton(text="🍌 200 генераций — 7 400 ₽", url=base_url200)],
        [InlineKeyboardButton(text="↩️ Назад в меню", callback_data="back_to_modes")]
    ])

    caption = (
        f"💼 <b>Ваш баланс:</b> {banans} генераций\n"
        f"🎁 Бесплатно: {FREE_DAILY_LIMIT}/день\n\n"
        f"🆔 <b>Ваш ID:</b> <code>{user.id}</code>\n"
        f"👤 <b>Ваш ник:</b> @{user.username or 'без ника'}\n\n"
        "⚠️ <b>ВАЖНО!</b>\n"
        "При оплате <u>обязательно</u> укажите ваш ID и ник в заказе.\n\n"
        "⏰ <b><u>ТОКЕНЫ НАЧИСЛЯЮТСЯ ПОСЛЕ РУЧНОЙ ПРОВЕРКИ ОПЛАТЫ НАШЕЙ ПОДДЕРЖКОЙ</u></b>\n\n"
        "👇 Выберите пакет генераций:"
    )


    if isinstance(message_or_callback, Message):
        await message_or_callback.answer_photo(photo=photo, caption=caption, reply_markup=kb)
    else:
        await message_or_callback.message.answer_photo(photo=photo, caption=caption, reply_markup=kb)



@router.message(Command("banans"))  
async def balance(message: Message):
    await show_balance(message, message.from_user.id)


@router.callback_query(F.data == "banans:banans")
async def handle_banans_callback(callback: CallbackQuery):
    await callback.answer()
    await show_balance(callback, callback.from_user.id)

@router.message(Command("img2img"))
async def img2img(message: Message):
    user_states[message.from_user.id] = {"mode": "img2img", "images": []}
    await message.answer("🖼️ Отправьте изображение, затем напишите промт.")


# ================== PHOTO ==================

@router.message(F.photo)
async def handle_photo(message: Message):
    user_id = message.from_user.id
    state = user_states.get(user_id)

    if not state or state.get("mode") != "img2img":
        return

    if "aspect_ratio" not in state:
        await message.answer("❗ Сначала выберите соотношение сторон.")
        return

    file = await bot.get_file(message.photo[-1].file_id)
    url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file.file_path}"

    state.setdefault("images", []).append(url)

    if len(state["images"]) == 1:
        await message.answer(
            "📸 Фото получено!\n\n"
            "➡️ Можете отправить ещё одно изображение\n"
            "✏️ Или напишите промт"
        )
    else:
        await message.answer("📸 Фото добавлено! Можете написать промт.")

# ================== TEXT / GENERATION ==================
@router.callback_query(F.data == "back_to_start")
async def back_to_start(callback: CallbackQuery):
    # Очищаем временные данные
    user_id = callback.from_user.id
    if user_id in user_states:
        user_states[user_id].pop("mode", None)
        user_states[user_id].pop("aspect_ratio", None)
        user_states[user_id].pop("images", None)
    
    # Показываем /start
    photo = FSInputFile("img/banana3.png")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖼️ Создать картинку с нуля", callback_data="select_mode:txt2img")],
        [InlineKeyboardButton(text="📷 Рдактировать ваше фото", callback_data="select_mode:img2img")],
    ])
    await callback.message.answer_photo(
        photo=photo,
        caption="🎨 <b>AI Image Generator</b>\n\nВыберите режим генерации:",
        reply_markup=kb
    )
    await callback.answer()
    

@router.message(F.text & ~F.text.startswith("/"))
async def generate(message: Message):
    prompt = message.text.strip()
    if not prompt:
        return

    gen_type = await can_generate(message.from_user.id)
    if not gen_type:
        photo = FSInputFile("img/no_tokens.png")
        await message.answer_photo(photo=photo, caption="❌ Генерации закончились. Используйте /banans.")
        return
    info_msg = await message.answer(
        "🪄 Генерация началась\n"
        "⏳ Обычно занимает ~20–40 секунд\n"
        "📸 Картинка придёт сразу после готовности"
    )

    try:
        user_id = message.from_user.id
        state = user_states.get(user_id, {})
        prompt_en = translate(prompt)
        aspect_ratio = state.get("aspect_ratio", "1:1")

        loop = asyncio.get_running_loop()
        if state.get("mode") == "img2img":
            output = await loop.run_in_executor(
                None,
                lambda: replicate.run(
                    "google/nano-banana-pro",
                    input={
                        "prompt": prompt_en,
                        "resolution": "2K",
                        "image_input": state["images"],
                        "output_format": "jpg",
                        "safety_filter_level": "block_low_and_above",
                        "aspect_ratio": aspect_ratio
                    }
                )
            )
            user_states.pop(user_id, None)

        else:
            output = await loop.run_in_executor(
                None,
                lambda: replicate.run(
                    "google/nano-banana",
                    input={
                        "prompt": prompt_en,
                        "aspect_ratio": aspect_ratio,
                        "output_format": "jpg",
                        "go_fast": True
                    }
                )
            )
        image_url = str(output)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Сгенерировать ещё", callback_data="back_to_start")]
        ])

        await bot.send_photo(
            chat_id=message.chat.id,
            photo=image_url,
            caption="✅ Готово!",
            reply_markup=kb
        )

        await finalize_generation(message.from_user.id, gen_type)
        try:
            await info_msg.delete()
        except:
            pass

    except Exception as e:
        try:
            await info_msg.delete()
        except:
            pass

        await message.answer(f"❌ Ошибка: <code>{str(e)[:300]}</code>")

# =========================================
ADMIN_IDS = {
    int(x)
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
@router.message(Command("add_tokens_for_users"))
async def add_tokens(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав.")
        return

    parts = message.text.split()
    if len(parts) != 3:
        await message.answer(
            "❌ Использование:\n"
            "/add_tokens_for_users <telegram_id> <кол-во>"
        )
        return

    try:
        target_id = int(parts[1])
        tokens = int(parts[2])
        if tokens <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Неверные параметры.")
        return

    async with aiosqlite.connect(DB_NAME, timeout=10) as db:
        cursor = await db.execute(
            "SELECT generation_tokens FROM users WHERE telegram_id = ?",
            (target_id,)
        )
        row = await cursor.fetchone()

        if not row:
            await message.answer("❌ Пользователь не найден.")
            return

        await db.execute(
            "UPDATE users SET generation_tokens = generation_tokens + ? WHERE telegram_id = ?",
            (tokens, target_id)
        )
        await db.commit()

    # ✅ Уведомление пользователю (ЛИЧНО)
    try:
        photo = FSInputFile("img/tokens.PNG")

        await bot.send_photo(
            chat_id=target_id,
            photo=photo,
            caption=(
                "🍌 <b>Баланс пополнен!</b>\n\n"
                f"Вам начислено <b>{tokens}</b> генераций.\n"
                "Спасибо за оплату ❤️\n\n"
                "Можете продолжать генерацию ✨"
            )
        )
        
    except Exception as e:
        # если пользователь не писал боту
        await message.answer(
            f"⚠️ Токены начислены, но не удалось уведомить пользователя.\n"
            f"Причина: {str(e)[:100]}"
        )
        return

    # ✅ Ответ админу
    await message.answer(
        f"✅ Начислено <b>{tokens}</b> генераций пользователю <code>{target_id}</code>"  
    )


@router.message(Command("users"))
async def list_users(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав.")
        return

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("""
            SELECT telegram_id, username, generation_tokens
            FROM users
            ORDER BY generation_tokens DESC
            LIMIT 50
        """)
        rows = await cursor.fetchall()

    if not rows:
        await message.answer("👀 Пользователей нет.")
        return

    text = "👥 <b>Пользователи:</b>\n\n"
    for uid, username, tokens in rows:
        text += (
            f"🆔 <code>{uid}</code>\n"
            f"👤 @{username or 'без ника'}\n"
            f"🍌 Токены: <b>{tokens}</b>\n\n"
        )

    await message.answer(text[:4000])

# ================== RUN ==================

dp.include_router(router)

async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.info("🚀 Bot started")
    asyncio.run(main())