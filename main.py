import asyncio
import os
import base64
import re
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BufferedInputFile
from openai import AsyncOpenAI
from dotenv import load_dotenv
from mosdiary import MOSDiaryClient

load_dotenv()

# ===== ОТКЛЮЧАЕМ ПРОВЕРКУ SSL (для локальных прокси) =====
os.environ["SSL_CERT_FILE"] = ""
os.environ["REQUESTS_CA_BUNDLE"] = ""
os.environ["CURL_CA_BUNDLE"] = ""

PROXY_URL = os.getenv("PROXY_URL")
MES_LINK = "https://school.mos.ru/diary/"

# ===== DEEPSEEK =====
ai_client = AsyncOpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
    timeout=60.0
)

dp = Dispatcher()
user_sessions = {}

SYSTEM_PROMPT = (
    "Ты — преподаватель колледжа. Реши задание и выдай КРАТКИЕ ответы списком. "
    "Формат: 'Вопрос 1: <ответ>' — без лишних объяснений, чтобы студент мог быстро ввести. "
    "Если это тест с вариантами — укажи букву или номер правильного варианта. "
    "В конце добавь короткое пояснение (1-2 предложения)."
)

class LoginState(StatesGroup):
    waiting_for_qr_confirm = State()
    waiting_for_code = State()

# Глобальная переменная бота
bot: Bot = None

# ===== СОЗДАНИЕ БОТА (внутри async) =====
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
        "👋 Привет! Я помогу с домашкой и тестами.\n\n"
        "🔐 /login — войти в МЭШ по QR-коду\n"
        "📚 /homework — показать домашки и решения\n"
        "📸 Отправь скриншот теста — выдам ответы списком\n"
        "🔗 Отправь ссылку на Google-форму — решу вопросы\n"
        "📝 Напиши текст задания — объясню решение\n\n"
        "⚠️ Я <b>не заполняю</b> тесты за тебя — я выписываю ответы в чат.\n"
        "Ты вводишь их в МЭШ сам."
    )

@dp.message(Command("login"))
async def cmd_login(message: types.Message, state: FSMContext):
    await message.answer("🔐 Генерирую QR-код для входа в МЭШ...")
    try:
        diary = MOSDiaryClient()
        qr_png, expires = await diary.start_qr_login(return_type="qr")

        await message.answer_photo(
            BufferedInputFile(qr_png, filename="mos_qr.png"),
            caption=(
                "📱 <b>Как войти:</b>\n\n"
                "1. Открой приложение «Дневник МЭШ» на телефоне.\n"
                "2. Перейди в «Настройки» → «Вход в Веб по QR».\n"
                "3. Отсканируй этот QR-код и подтверди вход.\n\n"
                "⏳ Я жду подтверждения..."
            )
        )

        await state.set_state(LoginState.waiting_for_qr_confirm)
        await state.update_data(diary=diary, expires=expires)

    except Exception as e:
        await message.answer(f"❌ Ошибка при генерации QR: {e}")
        await state.clear()

@dp.message(LoginState.waiting_for_qr_confirm)
async def process_qr_confirm(message: types.Message, state: FSMContext):
    data = await state.get_data()
    diary = data.get("diary")
    expires = data.get("expires")

    if not diary:
        await message.answer("⚠️ Сессия истекла. Начни заново: /login")
        await state.clear()
        return

    try:
        # Ждём подтверждения входа (без 2FA)
        aupd_token, refresh_token = await diary.wait_qr_login(expires)

        profile = await diary.get_base_info()

        user_sessions[message.from_user.id] = {
            "token": aupd_token,
            "refresh": refresh_token,
            "name": profile.full_name
        }

        await diary.close()
        await state.clear()

        await message.answer(
            f"✅ Вход выполнен, <b>{profile.full_name}</b>!\n\n"
            "Теперь напиши /homework — покажу домашки и решения.\n\n"
            "⚠️ Я только выписываю ответы в чат. Вводи их в МЭШ сам."
        )

    except Exception as e:
        await message.answer(
            f"❌ Вход не удался: {e}\n\n"
            "Попробуй /login заново."
        )
        await state.clear()

@dp.message(Command("logout"))
async def cmd_logout(message: types.Message):
    user_id = message.from_user.id
    if user_id in user_sessions:
        del user_sessions[user_id]
        await message.answer("✅ Ты вышел из МЭШ.")
    else:
        await message.answer("Ты и так не в системе.")

@dp.message(Command("homework"))
async def cmd_homework(message: types.Message):
    user_id = message.from_user.id
    session = user_sessions.get(user_id)

    if not session:
        await message.answer("⚠️ Сначала войди в МЭШ командой /login.")
        return

    await message.answer("📚 Загружаю домашние задания...")

    try:
        today = date.today()
        next_week = today + timedelta(days=7)

        diary = MOSDiaryClient(aupd_token=session["token"])
        homeworks = await diary.get_homework(today, next_week)
        await diary.close()

        if not homeworks:
            await message.answer("🎉 На ближайшую неделю домашек нет!")
            return

        text = f"📚 <b>Домашки для {session['name']}:</b>\n\n"
        tasks_for_ai = []

        for hw in homeworks:
            date_str = hw.date.strftime("%d.%m")
            text += f"📅 <b>{date_str}</b> | {hw.subject_name}:\n<i>{hw.homework}</i>\n\n"
            if hw.homework and hw.homework.strip():
                tasks_for_ai.append(
                    f"Предмет: {hw.subject_name}\nЗадание: {hw.homework}"
                )

        # Отправляем список домашек (обрезаем до 4000 символов)
        await message.answer(text[:4000])

        if tasks_for_ai:
            await message.answer("🧠 Решаю задания через DeepSeek...")
            prompt = "\n\n---\n\n".join(tasks_for_ai)

            response = await ai_client.chat.completions.create(
                model="deepseek-flash",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ]
            )
            solution = response.choices[0].message.content

            await message.answer(
                f"✍️ <b>Ответы (введи их в МЭШ сам):</b>\n\n{solution}"[:4000]
            )

    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}\nПопробуй /login заново.")

# ===== ТЕКСТ =====

@dp.message(F.text & ~F.text.startswith("http") & ~F.text.startswith("/"))
async def handle_text(message: types.Message):
    await message.answer("🧠 Решаю...")
    try:
        response = await ai_client.chat.completions.create(
            model="deepseek-flash",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": message.text}
            ]
        )
        await message.answer(response.choices[0].message.content[:4000])
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ===== ССЫЛКИ =====

@dp.message(F.text.startswith("http"))
async def handle_link(message: types.Message):
    url = message.text.strip()

    if "school.mos.ru" in url:
        await message.answer(
            "🔗 Это ссылка на МЭШ.\n\n"
            "Чтобы я помог с заданием:\n"
            "1. Открой эту ссылку в браузере и залогинься.\n"
            "2. Найди тест или задание.\n"
            "3. Сделай <b>скриншот</b> и пришли мне сюда.\n\n"
            "Я решу и выдам ответы списком.\n\n"
            "💡 Или используй /login, чтобы я видел твои домашки автоматически."
        )
        return

    await message.answer("🔗 Открываю форму...")
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        html = requests.get(url, headers=headers, timeout=15).text
        soup = BeautifulSoup(html, "html.parser")

        questions = []
        for tag in soup.find_all(["div", "span"], class_=re.compile("Title|Question|Text")):
            text = tag.get_text(strip=True)
            if text and len(text) > 3 and text not in questions:
                questions.append(text)

        if not questions:
            await message.answer("⚠️ Не смог найти вопросы. Пришли скриншот.")
            return

        prompt = "Реши вопросы из теста:\n\n" + "\n".join(questions)
        response = await ai_client.chat.completions.create(
            model="deepseek-flash",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ]
        )
        await message.answer(
            f"✍️ <b>Ответы (введи их в форму сам):</b>\n\n"
            f"{response.choices[0].message.content}"[:4000]
        )

    except Exception as e:
        await message.answer(f"❌ Не смог открыть ссылку: {e}")

# ===== ФОТО =====

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    await message.answer("📸 Читаю задание...")
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
                        {"type": "text", "text": "Реши задания на фото. Выдай ответы списком."},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}
                        }
                    ]
                }
            ]
        )
        await message.answer(
            f"✍️ <b>Ответы (введи их сам):</b>\n\n"
            f"{response.choices[0].message.content}"[:4000]
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка с фото: {e}")

# ===== ДОКУМЕНТЫ (PDF, DOCX) =====

@dp.message(F.document)
async def handle_document(message: types.Message):
    await message.answer("📄 Читаю файл...")
    try:
        doc = message.document
        file = await bot.get_file(doc.file_id)
        file_bytes = await bot.download_file(file.file_path)

        # Пробуем прочитать как текст
        try:
            content = file_bytes.read().decode("utf-8", errors="ignore")
        except Exception:
            await message.answer("⚠️ Не смог прочитать файл. Пришли скриншот.")
            return

        if len(content) < 10:
            await message.answer("⚠️ Файл пустой или не текстовый. Пришли скриншот.")
            return

        prompt = f"Реши задание из файла:\n\n{content[:3000]}"
        response = await ai_client.chat.completions.create(
            model="deepseek-flash",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ]
        )
        await message.answer(
            f"✍️ <b>Ответы:</b>\n\n{response.choices[0].message.content}"[:4000]
        )
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