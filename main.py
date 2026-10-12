import asyncio
import os
import base64

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

# ===== НАСТРОЙКИ =====
PROXY_URL = os.getenv("PROXY_URL")

# ===== DEEPSEEK =====
ai_client = AsyncOpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
    timeout=60.0
)

# ===== БОТ =====
dp = Dispatcher()

SYSTEM_PROMPT = (
    "Ты — преподаватель колледжа. Реши задание и дай подробное объяснение на русском. "
    "Распиши шаги решения, чтобы студент понял. В конце — краткий ответ."
)

bot: Bot = None

async def create_bot() -> Bot:
    if PROXY_URL:
        session = AiohttpSession(proxy=PROXY_URL)
        b = Bot(
            token=os.getenv("BOT_TOKEN"),
            session=session,
            default=DefaultBotProperties(parse_mode="HTML")
        )
    else:
        b = Bot(
            token=os.getenv("BOT_TOKEN"),
            default=DefaultBotProperties(parse_mode="HTML")
        )
    b.session.timeout = 60
    return b

# ===== КОМАНДЫ =====

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я решаю задания через DeepSeek.\n\n"
        "📝 Отправь текст задания — решу и объясню.\n"
        "📸 Отправь скриншот — прочитаю и решу.\n"
        "📄 Отправь PDF или DOCX — разберу и решу.\n\n"
        "⚠️ Я не захожу в твои аккаунты. Просто помогаю с заданиями."
    )

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📌 <b>Как пользоваться:</b>\n\n"
        "• Просто напиши задание текстом\n"
        "• Или пришли фото задания\n"
        "• Или пришли PDF/DOCX файл\n\n"
        "Я решу и объясню на русском."
    )

# ===== ТЕКСТ =====

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: types.Message):
    if not message.text or len(message.text.strip()) < 2:
        return

    await message.answer("🧠 Решаю...")
    try:
        response = await ai_client.chat.completions.create(
            model="deepseek-flash",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": message.text}
            ]
        )
        answer = response.choices[0].message.content
        # Разбиваем длинный ответ на части по 4000 символов
        for i in range(0, len(answer), 4000):
            await message.answer(answer[i:i + 4000])
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ===== ФОТО =====

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    await message.answer("📸 Читаю задание с фото...")
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        file_bytes = await bot.download_file(file.file_path)
        image_base64 = base64.b64encode(file_bytes.read()).decode("utf-8")

        response = await ai_client.chat.completions.create(
            model="deepseek-flash",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Прочитай задание на фото и реши его. Распиши шаги."},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}
                        }
                    ]
                }
            ]
        )
        answer = response.choices[0].message.content
        for i in range(0, len(answer), 4000):
            await message.answer(answer[i:i + 4000])
    except Exception as e:
        await message.answer(f"❌ Ошибка с фото: {e}")

# ===== ДОКУМЕНТЫ =====

@dp.message(F.document)
async def handle_document(message: types.Message):
    await message.answer("📄 Читаю файл...")
    try:
        doc = message.document
        file = await bot.get_file(doc.file_id)
        file_bytes = await bot.download_file(file.file_path)
        content = file_bytes.read()

        # Пробуем декодировать как текст
        try:
            text = content.decode("utf-8", errors="ignore")
        except Exception:
            await message.answer("⚠️ Не смог прочитать файл. Пришли скриншот.")
            return

        if len(text.strip()) < 10:
            await message.answer("⚠️ Файл пустой или содержит только картинки. Пришли скриншот.")
            return

        # Обрезаем до 3000 символов
        prompt = f"Реши задание из файла:\n\n{text[:3000]}"
        response = await ai_client.chat.completions.create(
            model="deepseek-flash",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ]
        )
        answer = response.choices[0].message.content
        for i in range(0, len(answer), 4000):
            await message.answer(answer[i:i + 4000])
    except Exception as e:
        await message.answer(f"❌ Ошибка с файлом: {e}")

# ===== ЗАПУСК =====

async def main():
    global bot
    bot = await create_bot()
    print("✅ Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
