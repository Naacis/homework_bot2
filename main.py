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

os.environ["SSL_CERT_FILE"] = ""
os.environ["REQUESTS_CA_BUNDLE"] = ""
os.environ["CURL_CA_BUNDLE"] = ""

PROXY_URL = os.getenv("PROXY_URL")
MES_LINK = "https://school.mos.ru/diary/"
MES_TIMEOUT = 120

ai_client = AsyncOpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
    timeout=60.0
)

dp = Dispatcher()
user_sessions = {}

# Хранилище "будущих" кодов подтверждения
pending_code_futures = {}

SYSTEM_PROMPT = (
    "Ты — преподаватель колледжа. Реши задание и выдай КРАТКИЕ ответы списком. "
    "Формат: 'Вопрос 1: <ответ>' — без лишних объяснений, чтобы студент мог быстро ввести. "
    "Если это тест с вариантами — укажи букву или номер правильного варианта. "
    "В конце добавь короткое пояснение (1-2 предложения)."
)

class LoginState(StatesGroup):
    waiting_for_code = State()

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
    await message.answer("🔐 Генерирую QR-код для входа в МЭШ... Это может занять до 2 минут.")

    try:
        diary = MOSDiaryClient()
        qr_png, expires = await asyncio.wait_for(
            diary.start_qr_login(return_type="qr"),
            timeout=MES_TIMEOUT
        )

        await message.answer_photo(
            BufferedInputFile(qr_png, filename="mos_qr.png"),
            caption=(
                "📱 <b>Как войти:</b>\n\n"
                "1. Открой приложение «Дневник МЭШ» на телефоне.\n"
                "2. Перейди в «Настройки» → «Вход в Веб по QR».\n"
                "3. Отсканируй этот QR-код и подтверди вход.\n\n"
                "⏳ Жду подтверждения... Если потребуется код из СМС — пришлю запрос."
            )
        )

        # Функция, которая вызовется, если Mos ID запросит код
        async def get_code() -> str:
            future = asyncio.get_event_loop().create_future()
            pending_code_futures[message.from_user.id] = future

            await message.answer(
                "🔑 <b>Mos ID требует код подтверждения.</b>\n\n"
                "Введи код из СМС (или последние 4 цифры входящего звонка)."
            )
            await state.set_state(LoginState.waiting_for_code)

            # Ждём, пока пользователь пришлёт код
            code = await future
            return code

        # Ждём подтверждения прямо здесь — не нужно отдельного обработчика
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
            "Теперь напиши /homework — покажу домашки и решения.\n\n"
            "⚠️ Я только выписываю ответы в чат. Вводи их в МЭШ сам."
        )

    except asyncio.TimeoutError:
        await message.answer("❌ МЭШ не ответил за 2 минуты. Попробуй /login заново.")
        await state.clear()
    except APIException as e:
        await message.answer(f"❌ Ошибка МЭШ: {e}\nПопробуй /login заново.")
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Ошибка при входе: {e}")
        await state.clear()

# Обработчик ввода кода подтверждения
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
        await message.answer("⚠️ Не жду никакого кода. Используй /login.")

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
        await message.answer("⚠️ Сначала войди в МЭШ командой /login.")
        return

    await message.answer("📚 Загружаю домашние задания... Это может занять до 2 минут.")

    try:
        today = date.today()
        next_week = today + timedelta(days=7)

        diary = MOSDiaryClient(aupd_token=session["token"])
        homeworks = await asyncio.wait_for(
            diary.get_homework(today, next_week),
            timeout=MES_TIMEOUT
        )
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

    except asyncio.TimeoutError:
        await message.answer("❌ МЭШ не ответил за 2 минуты. Попробуй /homework позже.")
    except APIException as e:
        await message.answer(f"❌ Ошибка МЭШ: {e}\nПопробуй /login заново.")
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

# ===== ЗАПУСК =====

async def main():
    global bot
    bot = await create_bot()
    print("✅ Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
