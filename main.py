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
from mosdiary.exceptions import APIException

load_dotenv()

# ===== НАСТРОЙКИ =====
PROXY_URL = os.getenv("PROXY_URL")
MES_LINK = "https://mos.ru"
MES_TIMEOUT = 120  # секунд ожидания ответа от МЭШ

# ===== SSL (для прокси) =====
# Если при запросах возникнут проблемы с SSL, закомментируйте или удалите эти 3 строки:
os.environ["SSL_CERT_FILE"] = ""
os.environ["REQUESTS_CA_BUNDLE"] = ""
os.environ["CURL_CA_BUNDLE"] = ""

# ===== DEEPSEEK =====
ai_client = AsyncOpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://deepseek.com",
    timeout=60.0
)

# ===== БОТ =====
dp = Dispatcher()
user_sessions = {}
pending_code_futures = {}

SYSTEM_PROMPT = (
    "Ты — преподаватель колледжа. Реши задание и выдай КРАТКИЕ ответы списком. "
    "Формат: 'Вопрос 1: <ответ>' — без лишних объяснений. "
    "Если это тест с вариантами — укажи букву или номер. "
    "В конце добавь короткое пояснение."
)

class LoginState(StatesGroup):
    waiting_for_code = State()

bot: Bot = None

# ===== СОЗДАНИЕ БОТА =====
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
        "📸 Отправь скриншот теста — выдам ответы\n"
        "🔗 Отправь ссылку на Google-форму — решу\n"
        "📝 Напиши текст задания — объясню\n\n"
        "⚠️ Я не заполняю тесты за тебя — выписываю ответы в чат."
    )

@dp.message(Command("login"))
async def cmd_login(message: types.Message, state: FSMContext):
    await message.answer("🔐 Генерирую QR-код... Это может занять до 2 минут.")

    try:
        # Передаем PROXY_URL в клиент МЭШ для обхода геоблока Railway
        diary = MOSDiaryClient(proxy=PROXY_URL)
        qr_png, expires = await asyncio.wait_for(
            diary.start_qr_login(return_type="qr"),
            timeout=MES_TIMEOUT
        )

        await message.answer_photo(
            BufferedInputFile(qr_png, filename="mos_qr.png"),
            caption=(
                "📱 <b>Как войти:</b>\n\n"
                "1. Открой приложение «Дневник МЭШ»\n"
                "2. Настройки → «Вход в Веб по QR»\n"
                "3. Отсканируй QR и подтверди вход\n\n"
                "⏳ Жду подтверждения..."
            )
        )

        # Функция для запроса кода подтверждения (2FA)
        async def get_code() -> str:
            future = asyncio.get_event_loop().create_future()
            pending_code_futures[message.from_user.id] = future

            await message.answer(
                "🔑 <b>Mos ID требует код.</b>\n\n"
                "Введи код из СМС (или последние 4 цифры звонка)."
            )
            await state.set_state(LoginState.waiting_for_code)

            code = await future
            return code

        aupd_token, refresh_token = await asyncio.wait_for(
            diary.wait_qr_login(
                expires,
                get_code=get_code,
                verification_method="sms",
                trust_browser=False,
            ),
            timeout=MES_TIMEOUT
        )

        profile = await asyncio.wait_for(
            diary.get_base_info(),
            timeout=MES_TIMEOUT
        )

        user_sessions[message.from_user.id] = {
            "token": aupd_token,
            "refresh": refresh_token,
            "name": profile.full_name
        }

        await diary.close()
        await state.clear()

        await message.answer(
            f"✅ Вход выполнен, <b>{profile.full_name}</b>!\n\n"
            "Напиши /homework — покажу домашки и решения."
        )

    except asyncio.TimeoutError:
        await message.answer("❌ МЭШ не ответил за 2 минуты. Попробуй /login заново.")
        await state.clear()
    except APIException as e:
        await message.answer(f"❌ Ошибка МЭШ: {e}")
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        await state.clear()

@dp.message(LoginState.waiting_for_code)
async def process_code(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    code = (message.text or "").strip()

    if not code:
        await message.answer("⚠️ Введи код текстом.")
        return

    future = pending_code_futures.get(user_id)
    if future and not future.done():
        future.set_result(code)
        del pending_code_futures[user_id]
        await message.answer("⏳ Проверяю код...")
    else:
        await message.answer("⚠️ Не жду код. Используй /login.")

    await state.clear()

@dp.message(Command("logout"))
async def cmd_logout(message: types.Message):
    if message.from_user.id in user_sessions:
        del user_sessions[message.from_user.id]
        await message.answer("✅ Ты вышел из МЭШ.")
    else:
        await message.answer("Ты и так не в системе.")

@dp.message(Command("homework"))
async def cmd_homework(message: types.Message):
    user_id = message.from_user.id
    session = user_sessions.get(user_id)

    if not session:
        await message.answer("⚠️ Сначала /login.")
        return

    await message.answer("📚 Загружаю домашки... Это займёт до 2 минут.")

    try:
        today = date.today()
        next_week = today + timedelta(days=7)

        # Здесь также обязательно передаем PROXY_URL
        diary = MOSDiaryClient(aupd_token=session["token"], proxy=PROXY_URL)
        homeworks = await asyncio.wait_for(
            diary.get_homework(today, next_week),
            timeout=MES_TIMEOUT
        )
        await diary.close()

        if not homeworks:
            await message.answer("🎉 Домашек нет!")
            return

        text = f"📚 <b>Домашки для {session['name']}:</b>\n\n"
        tasks_for_ai = []

        for hw in homeworks:
            date_str = hw.date.strftime("%d.%m")
            text += f"📅 <b>{date_str}</b> | {hw.subject_name}:\n<i>{hw.homework}</i>\n\n"
            if hw.homework and hw.homework.strip():
                tasks_for_ai.append(f"Предмет: {hw.subject_name}\nЗадание: {hw.homework}")

        await message.answer(text[:4000])

        if tasks_for_ai:
            await message.answer("🧠 Решаю через DeepSeek...")
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

    except asyncio.TimeoutError:
        await message.answer("❌ МЭШ не ответил. Попробуй позже.")
    except APIException as e:
        await message.answer(f"❌ Ошибка МЭШ: {e}\nПопробуй /login.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

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
        solution = response.choices[0].message.content
        await message.answer(f"✍️ <b>Ответ:</b>\n\n{solution}")
    except Exception as e:
        await message.answer(f"❌ Ошибка нейросети: {e}")

# ===== ЗАПУСК БОТА =====
async def main():
    global bot
    bot = await create_bot()
    print("Бот успешно запущен!")
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
