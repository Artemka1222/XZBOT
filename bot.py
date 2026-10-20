"""
Telegram-бот: создаёт премиум-эмодзи с текстом на градиентном фоне.

Как работает:
  1. Пользователь отправляет /emoji <текст> (до 20 символов, русский или английский).
  2. Бот предлагает выбрать градиент кнопками.
  3. Бот рисует эмодзи 100x100 (WEBP) через Pillow и добавляет его в личный
     набор премиум-эмодзи пользователя (custom_emoji).
  4. Пользователь получает готовое премиум-эмодзи и ссылку на набор.

Почему не редактируем чужой пак: в EmojiStriver текст нарисован фигурами,
а не текстовыми слоями, поэтому заменить его в файле нельзя. Этот бот
рисует текст сам, поэтому работает независимо от структуры пака.

Требования:
  pip install "aiogram>=3.31" pillow
  BOT_TOKEN=... python bot_v2.py

Необязательно:
  FONT_PATH=/путь/к/шрифту.ttf   — свой шрифт с кириллицей
  MENU_GIF_ID=...                — file_id анимации для меню
  STORE_FILE=store.json          — где хранить личные наборы
"""

import asyncio
import html
import io
import json
import logging
import os
import re
import secrets
from functools import lru_cache

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputSticker,
    Message,
)
from PIL import Image, ImageDraw, ImageFont

# ============================================================
#  НАСТРОЙКИ
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MENU_GIF_ID = os.getenv("MENU_GIF_ID", "").strip()
STORE_FILE = os.getenv("STORE_FILE", "store.json").strip()
FONT_PATH = os.getenv("FONT_PATH", "").strip()

MAX_TEXT_LEN = 20
EMOJI_SIZE = 100            # премиум-эмодзи в Telegram: 100x100
SUPERSAMPLE = 4             # рисуем в 4x и уменьшаем, чтобы текст был чётким
MAX_STICKER_BYTES = 60 * 1024

# Градиенты: ключ -> (название кнопки, цвет вверху, цвет внизу)
GRADIENTS: dict[str, tuple[str, tuple[int, int, int], tuple[int, int, int]]] = {
    "fire": ("Огонь", (255, 64, 32), (255, 200, 60)),
    "ocean": ("Океан", (0, 90, 255), (0, 230, 255)),
    "violet": ("Фиолет", (110, 40, 240), (255, 60, 190)),
    "mint": ("Мята", (0, 190, 140), (120, 255, 150)),
    "gold": ("Золото", (255, 150, 0), (255, 240, 120)),
    "night": ("Ночь", (20, 20, 60), (90, 60, 160)),
}

# Шрифты с кириллицей. Берётся первый найденный.
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/system/fonts/Roboto-Bold.ttf",
    "/system/fonts/DroidSans-Bold.ttf",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("emoji_bot")

router = Router()

# str(user_id) -> список имён личных наборов (последний — активный)
own_sets: dict[str, list[str]] = {}
# user_id -> текст, ждущий выбора градиента
pending: dict[int, str] = {}
# один пользователь — одна запись в набор за раз
user_locks: dict[int, asyncio.Lock] = {}
_bot_username: str | None = None


def lock_for(user_id: int) -> asyncio.Lock:
    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()
    return user_locks[user_id]


# ============================================================
#  ХРАНИЛИЩЕ
# ============================================================
def load_store() -> None:
    global own_sets
    try:
        with open(STORE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        own_sets = {str(k): list(v) for k, v in data.get("own_sets", {}).items()}
        log.info("Загружено личных наборов: %d", len(own_sets))
    except FileNotFoundError:
        own_sets = {}
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as e:
        log.warning("Не смог прочитать %s (%s), начинаю с пустого", STORE_FILE, e)
        own_sets = {}


def save_store() -> None:
    tmp = STORE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"own_sets": own_sets}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE_FILE)


# ============================================================
#  ПРЕМИУМ-ЭМОДЗИ ДЛЯ ОФОРМЛЕНИЯ (ID только из emoji.wivvi.net/llms.txt)
# ============================================================
E_COMMANDS = "5962952497197748583"  # 🔧 Команды /
E_FOLDER = "6039630677182254664"    # 📂 Папка
E_EDIT = "6039779802741739617"      # ✏️ Редактировать
E_GIFT = "6032644646587338669"      # 🎁 Подарок
E_CODE = "5940433880585605708"      # 🔨 Код </>
E_INFO = "6028435952299413210"      # ℹ Инфо
E_OK = "5774022692642492953"        # ✅ Галочка
E_ERR = "5774077015388852135"       # ❌ Крестик
E_WARN = "6030563507299160824"      # ❗️ Внимание
E_TRASH = "6039522349517115015"     # 🗑 Мусорный бак


def pe(emoji_id: str | None, fallback: str) -> str:
    """Премиум-эмодзи для HTML-текста. Без ID — обычный эмодзи."""
    fb = html.escape(fallback)
    if not emoji_id:
        return fb
    return f'<tg-emoji emoji-id="{emoji_id}">{fb}</tg-emoji>'


def ok(text: str) -> str:
    return f"{pe(E_OK, '✅')} {text}"


def err(text: str) -> str:
    return f"{pe(E_ERR, '❌')} {text}"


def warn(text: str) -> str:
    return f"{pe(E_WARN, '❗️')} {text}"


def btn(text: str, emoji_id: str | None = None, callback_data: str | None = None,
        url: str | None = None) -> InlineKeyboardButton:
    """Кнопка. Иконка — через icon_custom_emoji_id, в тексте обычных эмодзи нет."""
    return InlineKeyboardButton(
        text=text,
        icon_custom_emoji_id=emoji_id,
        callback_data=callback_data,
        url=url,
    )


HELP_TEXT = (
    f"{pe(E_COMMANDS, '🔧')} <b>Премиум-эмодзи с текстом</b>\n"
    f"{pe(E_EDIT, '✏️')} /emoji &lt;текст&gt; — создать эмодзи (до {MAX_TEXT_LEN} символов)\n"
    f"{pe(E_GIFT, '🎁')} /myset — мой набор эмодзи\n"
    f"{pe(E_TRASH, '🗑')} /delete &lt;номер&gt; — удалить эмодзи из набора\n"
    f"{pe(E_CODE, '🔨')} /ids — ответом на сообщение с эмодзи показать ID\n"
    f"{pe(E_INFO, 'ℹ')} /help — эта справка"
)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("Создать эмодзи", E_EDIT, callback_data="menu:new")],
        [
            btn("Мой набор", E_GIFT, callback_data="menu:myset"),
            btn("Помощь", E_INFO, callback_data="menu:help"),
        ],
    ])


def gradient_markup() -> InlineKeyboardMarkup:
    items = [(key, title) for key, (title, _, _) in GRADIENTS.items()]
    rows = [
        [btn(items[i][1], callback_data=f"grad:{items[i][0]}")]
        + ([btn(items[i + 1][1], callback_data=f"grad:{items[i + 1][0]}")] if i + 1 < len(items) else [])
        for i in range(0, len(items), 2)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def open_set_markup(name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("Открыть набор", E_FOLDER, url=f"https://t.me/addemoji/{name}")]
    ])


async def send_menu(target: Message, text: str, markup: InlineKeyboardMarkup) -> None:
    """Меню с GIF, если задан MENU_GIF_ID, иначе текстом."""
    if MENU_GIF_ID:
        try:
            await target.answer_animation(MENU_GIF_ID, caption=text, reply_markup=markup)
            return
        except TelegramAPIError as e:
            log.warning("GIF меню не отправился (%s), шлю текстом", e)
    await target.answer(text, reply_markup=markup)


# ============================================================
#  РИСОВАНИЕ ЭМОДЗИ
# ============================================================
class EmojiError(Exception):
    """Понятная пользователю ошибка."""


@lru_cache(maxsize=1)
def find_font_path() -> str | None:
    for p in ([FONT_PATH] if FONT_PATH else []) + FONT_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return None


@lru_cache(maxsize=64)
def get_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = find_font_path()
    if path:
        return ImageFont.truetype(path, size)
    # Без системного шрифта — встроенный Pillow (латиница). Кириллица может не отображаться.
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _lanczos():
    return getattr(Image, "Resampling", Image).LANCZOS


def validate_text(raw: str) -> str:
    text = " ".join(raw.split())  # схлопываем пробелы и переносы
    if not text:
        raise EmojiError("Текст не может быть пустым")
    if len(text) > MAX_TEXT_LEN:
        raise EmojiError(f"Текст длиннее {MAX_TEXT_LEN} символов")
    return text


def render_emoji(text: str, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> bytes:
    """Рисует эмодзи 100x100: градиентный скруглённый фон и текст. Возвращает WEBP."""
    size = EMOJI_SIZE * SUPERSAMPLE

    # 1. вертикальный градиент
    gradient = Image.new("RGBA", (size, size))
    gd = ImageDraw.Draw(gradient)
    for y in range(size):
        t = y / (size - 1)
        color = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,)
        gd.line([(0, y), (size, y)], fill=color)

    # 2. скруглённая маска
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=size // 5, fill=255
    )
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(gradient, (0, 0), mask)

    # 3. текст: подбираем размер, чтобы влез в плашку
    draw = ImageDraw.Draw(canvas)
    stroke = max(2, size // 70)
    max_w = int(size * 0.86)
    font_size = int(size * 0.5)
    font = get_font(font_size)
    while font_size > 24:
        left, top_, right, bottom_ = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
        if right - left <= max_w:
            break
        font_size -= 6
        font = get_font(font_size)

    left, top_, right, bottom_ = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
    w, h = right - left, bottom_ - top_
    x = (size - w) // 2 - left
    y = (size - h) // 2 - top_
    draw.text(
        (x, y), text, font=font,
        fill=(255, 255, 255, 255),
        stroke_width=stroke, stroke_fill=(0, 0, 0, 110),
    )

    # 4. в 100x100 и сохранение
    img = canvas.resize((EMOJI_SIZE, EMOJI_SIZE), _lanczos())
    buf = io.BytesIO()
    img.save(buf, "WEBP", lossless=True)
    data = buf.getvalue()
    if len(data) > MAX_STICKER_BYTES:
        buf = io.BytesIO()
        img.save(buf, "WEBP", quality=85)
        data = buf.getvalue()
    if len(data) > MAX_STICKER_BYTES:
        raise EmojiError("Эмодзи получился слишком большим, сделай текст короче")
    return data


# ============================================================
#  TELEGRAM: ЛИЧНЫЕ НАБОРЫ
# ============================================================
async def bot_username(bot: Bot) -> str:
    global _bot_username
    if _bot_username is None:
        me = await bot.get_me()
        _bot_username = me.username or ""
    return _bot_username


async def add_to_own_set(bot: Bot, user_id: int, webp: bytes, emoji: str = "✨") -> str:
    """
    Добавляет эмодзи в личный набор. Если набор заполнен или недоступен,
    создаёт новый. Возвращает имя набора.
    """

    def make_sticker() -> InputSticker:
        return InputSticker(
            sticker=BufferedInputFile(webp, filename="emoji.webp"),
            format="static",
            emoji_list=[emoji],
        )

    key = str(user_id)
    sets = own_sets.setdefault(key, [])

    if sets:
        name = sets[-1]
        try:
            await bot.add_sticker_to_set(user_id=user_id, name=name, sticker=make_sticker())
            return name
        except TelegramAPIError as e:
            log.info("Не смог добавить в %s (%s), создаю новый набор", name, e)

    username = await bot_username(bot)
    name = f"t{user_id}_{secrets.token_hex(3)}_by_{username}"
    await bot.create_new_sticker_set(
        user_id=user_id,
        name=name,
        title="My text emoji",
        stickers=[make_sticker()],
        sticker_type="custom_emoji",
    )
    sets.append(name)
    save_store()
    return name


async def last_custom_id(bot: Bot, name: str) -> str | None:
    s = await bot.get_sticker_set(name)
    return s.stickers[-1].custom_emoji_id if s.stickers else None


async def create_and_send(target: Message, bot: Bot, user_id: int, text: str, grad_key: str) -> None:
    """Полный цикл: нарисовать -> добавить в набор -> отправить результат."""
    _, top, bottom = GRADIENTS[grad_key]
    try:
        webp = await asyncio.to_thread(render_emoji, text, top, bottom)
        async with lock_for(user_id):
            name = await add_to_own_set(bot, user_id, webp)
        custom_id = await last_custom_id(bot, name)
    except EmojiError as e:
        return await target.answer(err(html.escape(str(e))))
    except TelegramAPIError as e:
        log.warning("Telegram отклонил эмодзи: %s", e)
        return await target.answer(err(f"Telegram отклонил эмодзи: {html.escape(str(e))}"))
    except Exception:
        log.exception("Непредвиденная ошибка при создании эмодзи")
        return await target.answer(err("Внутренняя ошибка, попробуй ещё раз"))

    if custom_id is None:
        return await target.answer(err("Эмодзи добавлен, но не удалось получить его ID"))

    await target.answer(
        ok(f"Готово: {pe(custom_id, '✨')}"),
        reply_markup=open_set_markup(name),
    )


async def show_myset(target: Message, user_id: int, bot: Bot) -> None:
    sets = own_sets.get(str(user_id))
    if not sets:
        return await target.answer(warn("Набора пока нет, создай эмодзи через /emoji"))
    name = sets[-1]
    try:
        s = await bot.get_sticker_set(name)
    except TelegramAPIError as e:
        return await target.answer(err(f"Не смог открыть набор: {html.escape(str(e))}"))
    await target.answer(
        f"{pe(E_GIFT, '🎁')} <b>Мой набор</b>\n"
        f"Эмодзи в наборе: {len(s.stickers)}",
        reply_markup=open_set_markup(name),
    )


# ============================================================
#  ХЭНДЛЕРЫ
# ============================================================
@router.message(Command("start", "help"))
async def cmd_start(message: Message):
    await send_menu(message, HELP_TEXT, main_menu())


@router.message(Command("emoji"))
async def cmd_emoji(message: Message, command: CommandObject):
    if not command.args:
        return await message.answer(warn(f"Использование: /emoji Привет (до {MAX_TEXT_LEN} символов)"))
    try:
        text = validate_text(command.args)
    except EmojiError as e:
        return await message.answer(warn(html.escape(str(e))))

    pending[message.from_user.id] = text
    await message.answer(
        f"{pe(E_EDIT, '✏️')} Текст: <b>{html.escape(text)}</b>\nВыбери фон:",
        reply_markup=gradient_markup(),
    )


@router.callback_query(F.data.startswith("grad:"))
async def on_gradient(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    key = (callback.data or "").split(":", 1)[1]
    text = pending.pop(user_id, None)
    if text is None or key not in GRADIENTS:
        # всплывающий текст без премиум-эмодзи (по гайду)
        return await callback.answer("Сначала отправь /emoji текст", show_alert=True)

    await callback.answer()
    msg = callback.message
    if isinstance(msg, Message):
        await create_and_send(msg, bot, user_id, text, key)


@router.callback_query(F.data.startswith("menu:"))
async def on_menu(callback: CallbackQuery, bot: Bot):
    action = (callback.data or "").split(":", 1)[1]
    await callback.answer()
    msg = callback.message
    if not isinstance(msg, Message):
        return
    if action == "new":
        await msg.answer(f"{pe(E_EDIT, '✏️')} Отправь: /emoji Привет")
    elif action == "myset":
        await show_myset(msg, callback.from_user.id, bot)
    elif action == "help":
        await send_menu(msg, HELP_TEXT, main_menu())


@router.message(Command("myset"))
async def cmd_myset(message: Message, bot: Bot):
    await show_myset(message, message.from_user.id, bot)


@router.message(Command("delete"))
async def cmd_delete(message: Message, command: CommandObject, bot: Bot):
    user_id = message.from_user.id
    sets = own_sets.get(str(user_id))
    if not sets:
        return await message.answer(warn("Набора пока нет"))

    arg = (command.args or "").strip()
    if not arg.isdigit():
        return await message.answer(warn("Использование: /delete 0 (номер эмодзи в наборе)"))

    try:
        s = await bot.get_sticker_set(sets[-1])
    except TelegramAPIError as e:
        return await message.answer(err(f"Не смог открыть набор: {html.escape(str(e))}"))

    idx = int(arg)
    if idx >= len(s.stickers):
        return await message.answer(err("Нет эмодзи с таким номером"))

    try:
        async with lock_for(user_id):
            await bot.delete_sticker_from_set(s.stickers[idx].file_id)
    except TelegramAPIError as e:
        return await message.answer(err(f"Не смог удалить: {html.escape(str(e))}"))
    await message.answer(ok("Удалил"))


@router.message(Command("ids"))
async def cmd_ids(message: Message):
    target = message.reply_to_message
    if not target:
        return await message.answer(warn("Ответь командой /ids на сообщение с эмодзи"))
    entities = (target.entities or []) + (target.caption_entities or [])
    ids = [e.custom_emoji_id for e in entities if e.type == "custom_emoji" and e.custom_emoji_id]
    if not ids:
        return await message.answer(err("В сообщении нет премиум-эмодзи"))
    await message.answer(
        f"{pe(E_CODE, '🔨')} <b>custom_emoji_id:</b>\n" + "\n".join(f"<code>{i}</code>" for i in ids)
    )


# ============================================================
#  ЗАПУСК
# ============================================================
async def main() -> None:
    global _bot_username
    if not BOT_TOKEN:
        raise SystemExit("Задай переменную окружения BOT_TOKEN")

    if find_font_path() is None and not FONT_PATH:
        log.warning("Системный шрифт не найден: кириллица может не отображаться. Задай FONT_PATH")
    else:
        log.info("Шрифт: %s", FONT_PATH or find_font_path())

    load_store()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    _bot_username = me.username or ""
    log.info("Бот запущен: @%s", _bot_username)

    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
