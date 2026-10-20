import asyncio
import gzip
import html
import io
import json
import logging
import os
import re
import secrets
from collections import Counter

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputSticker,
    Message,
)

# ============================================================
#  НАСТРОЙКИ
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# file_id анимации для меню (необязательно). Пусто — меню текстом.
MENU_GIF_ID = os.getenv("MENU_GIF_ID", "").strip()
# Файл, где хранятся личные наборы пользователей (переживает перезапуск)
STORE_FILE = os.getenv("STORE_FILE", "store.json").strip()

MAX_TEXT_LEN = 20                # максимальная длина нового текста
MAX_TGS_BYTES = 64 * 1024        # лимит Telegram на TGS-стикер
NEEDS_REPAINTING = False         # True — эмодзи перекрасится в цвет текста (градиенты пропадут)
LIST_LIMIT = 60                  # сколько эмодзи показывать в /list
PACK_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

LOTTIE_TEXT_LAYER = 5            # ty = 5 у текстового слоя в Lottie
LAYER_NAMES = {0: "precomp", 1: "solid", 2: "image", 3: "null", 4: "shape", 5: "text"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("emoji_bot")

router = Router()

# user_id (int) -> {"pack": str, "stickers": list[Sticker]}
loaded: dict[int, dict] = {}
# str(user_id) -> список имён личных наборов (последний — активный)
own_sets: dict[str, list[str]] = {}
# один пользователь — одна операция записи в набор за раз
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
#  ПРЕМИУМ-ЭМОДЗИ (ID только из emoji.wivvi.net/llms.txt)
# ============================================================
E_COMMANDS = "5962952497197748583"  # 🔧 Команды /
E_FOLDER = "6039630677182254664"    # 📂 Папка
E_LIST = "5766994197705921104"      # 🗂 Список
E_EDIT = "6039779802741739617"      # ✏️ Редактировать
E_GIFT = "6032644646587338669"      # 🎁 Подарок
E_TRASH = "6039522349517115015"     # 🗑 Мусорный бак
E_CODE = "5940433880585605708"      # 🔨 Код </>
E_INFO = "6028435952299413210"      # ℹ Инфо
E_OK = "5774022692642492953"        # ✅ Галочка
E_ERR = "5774077015388852135"       # ❌ Крестик
E_WARN = "6030563507299160824"      # ❗️ Внимание
E_DEBUG = "5904219717073114606"     # 📁 Просмотр файла


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


def btn(text: str, emoji_id: str, callback_data: str | None = None, url: str | None = None) -> InlineKeyboardButton:
    """Кнопка с премиум-иконкой. В тексте кнопки обычных эмодзи нет."""
    return InlineKeyboardButton(
        text=text,
        icon_custom_emoji_id=emoji_id,
        callback_data=callback_data,
        url=url,
    )


HELP_TEXT = (
    f"{pe(E_COMMANDS, '🔧')} <b>Команды</b>\n"
    f"{pe(E_FOLDER, '📂')} /load &lt;ссылка или имя&gt; — загрузить пак\n"
    f"{pe(E_LIST, '🗂')} /list — эмодзи пака с номерами\n"
    f"{pe(E_EDIT, '✏️')} /replace &lt;номер&gt; &lt;текст&gt; — заменить текст\n"
    f"{pe(E_GIFT, '🎁')} /myset — мой личный набор\n"
    f"{pe(E_TRASH, '🗑')} /delete &lt;номер&gt; — удалить из моего набора\n"
    f"{pe(E_CODE, '🔨')} /ids — ответом на сообщение с эмодзи\n"
    f"{pe(E_INFO, 'ℹ')} /info &lt;пак&gt; — информация о паке\n"
    f"{pe(E_DEBUG, '📁')} /debug &lt;номер&gt; — типы слоёв эмодзи"
)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("Загрузить пак", E_FOLDER, callback_data="menu:load")],
        [
            btn("Список", E_LIST, callback_data="menu:list"),
            btn("Мой набор", E_GIFT, callback_data="menu:myset"),
        ],
        [btn("Помощь", E_INFO, callback_data="menu:help")],
    ])


def open_set_markup(name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("Открыть набор", E_FOLDER, url=f"https://t.me/addemoji/{name}")]
    ])


async def send_menu(target: Message, text: str, markup: InlineKeyboardMarkup) -> None:
    """Меню с GIF, если задан MENU_GIF_ID, иначе обычным текстом."""
    if MENU_GIF_ID:
        try:
            await target.answer_animation(MENU_GIF_ID, caption=text, reply_markup=markup)
            return
        except TelegramAPIError as e:
            log.warning("GIF меню не отправился (%s), шлю текстом", e)
    await target.answer(text, reply_markup=markup)


# ============================================================
#  РАБОТА С ПАКАМИ И ФАЙЛАМИ
# ============================================================
class EmojiError(Exception):
    """Понятная пользователю ошибка (текст уйдёт в ответ как есть)."""


def pack_name_from(arg: str) -> str:
    """'EmojiStriver' или 'https://t.me/addemoji/EmojiStriver?x=1' -> 'EmojiStriver'."""
    cleaned = arg.strip().split("?")[0].rstrip("/")
    return cleaned.split("/")[-1]


async def bot_username(bot: Bot) -> str:
    global _bot_username
    if _bot_username is None:
        me = await bot.get_me()
        _bot_username = me.username or ""
    return _bot_username


async def download_bytes(bot: Bot, sticker) -> bytes:
    buf = io.BytesIO()
    await bot.download(sticker, destination=buf)
    return buf.getvalue()


def _set_text(doc, text: str) -> int:
    """Меняет строку в объекте TextDocument. Возвращает 1, если заменено."""
    if isinstance(doc, dict) and isinstance(doc.get("t"), str):
        doc["t"] = text
        return 1
    return 0


def replace_text_in_lottie(node, text: str) -> int:
    """
    Рекурсивно обходит Lottie и меняет текст во всех текстовых слоях (ty == 5).

    В Lottie текст лежит в layer["t"]["d"]["k"]. Бывает два варианта:
      - статичный текст: k — это сам TextDocument с полем "t";
      - ключевые кадры: k — список, а в каждом кадре TextDocument лежит в "s".
    Старый код смотрел только на второй вариант, поэтому статичный текст не находил.
    """
    count = 0
    if isinstance(node, dict):
        if node.get("ty") == LOTTIE_TEXT_LAYER and isinstance(node.get("t"), dict):
            d = node["t"].get("d")
            k = d.get("k") if isinstance(d, dict) else None
            if isinstance(k, dict):
                count += _set_text(k, text)
            elif isinstance(k, list):
                for kf in k:
                    if not isinstance(kf, dict):
                        continue
                    s = kf.get("s")
                    if isinstance(s, dict):
                        count += _set_text(s, text)
                    elif isinstance(s, list):
                        for doc in s:
                            count += _set_text(doc, text)
        for value in node.values():
            if isinstance(value, (dict, list)):
                count += replace_text_in_lottie(value, text)
    elif isinstance(node, list):
        for item in node:
            count += replace_text_in_lottie(item, text)
    return count


def collect_layer_types(node, counter: Counter) -> None:
    """Считает слои по типу (для диагностики через /debug)."""
    if isinstance(node, dict):
        ty = node.get("ty")
        if isinstance(ty, int) and "ind" in node:
            counter[ty] += 1
        for value in node.values():
            if isinstance(value, (dict, list)):
                collect_layer_types(value, counter)
    elif isinstance(node, list):
        for item in node:
            collect_layer_types(item, counter)


def decode_tgs(raw: bytes) -> dict:
    try:
        return json.loads(gzip.decompress(raw))
    except (OSError, EOFError, ValueError) as e:
        raise EmojiError(f"Не смог прочитать TGS-файл эмодзи ({e.__class__.__name__})")


def make_text_tgs(raw: bytes, text: str) -> bytes:
    """Из TGS-байтов делает новый TGS с другим текстом. Бросает EmojiError при проблеме."""
    obj = decode_tgs(raw)
    changed = replace_text_in_lottie(obj, text)
    if changed == 0:
        raise EmojiError(
            "В этом эмодзи нет текстовых слоёв, поэтому текст поменять нельзя. "
            "Смотри /debug, там видно, какие слои внутри"
        )
    out = gzip.compress(
        json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if len(out) > MAX_TGS_BYTES:
        raise EmojiError(
            f"Получился TGS {len(out) // 1024} КБ, а лимит Telegram 64 КБ. Возьми текст короче"
        )
    return out


async def add_to_own_set(bot: Bot, user_id: int, tgs: bytes, emoji: str) -> str:
    """
    Добавляет эмодзи в личный набор пользователя.
    Если активный набор заполнен или недоступен — создаёт новый.
    Возвращает имя набора, куда попало эмодзи.
    """

    def make_sticker() -> InputSticker:
        return InputSticker(
            sticker=BufferedInputFile(tgs, filename="emoji.tgs"),
            format="animated",
            emoji_list=[emoji],
        )

    key = str(user_id)
    sets = own_sets.setdefault(key, [])

    if sets:
        name = sets[-1]
        try:
            await bot.add_sticker_to_set(user_id=user_id, name=name, sticker=make_sticker())
            return name
        except TelegramBadRequest as e:
            log.info("Не смог добавить в %s (%s), создаю новый набор", name, e)

    username = await bot_username(bot)
    name = f"t{user_id}_{secrets.token_hex(3)}_by_{username}"
    await bot.create_new_sticker_set(
        user_id=user_id,
        name=name,
        title="My text emoji",
        stickers=[make_sticker()],
        sticker_type="custom_emoji",
        needs_repainting=NEEDS_REPAINTING,
    )
    sets.append(name)
    save_store()
    return name


async def last_custom_id(bot: Bot, name: str) -> str | None:
    s = await bot.get_sticker_set(name)
    return s.stickers[-1].custom_emoji_id if s.stickers else None


async def show_list(target: Message, user_id: int) -> None:
    data = loaded.get(user_id)
    if not data:
        return await target.answer(err("Сначала загрузи пак: /load EmojiStriver"))
    rows = [
        f"{i}. {pe(st.custom_emoji_id, st.emoji or '•')}"
        for i, st in enumerate(data["stickers"][:LIST_LIMIT])
    ]
    text = f"{pe(E_LIST, '🗂')} <b>Эмодзи пака</b>\n" + "\n".join(rows)
    extra = len(data["stickers"]) - LIST_LIMIT
    if extra > 0:
        text += f"\n… и ещё {extra}"
    await target.answer(text)


async def show_myset(target: Message, user_id: int, bot: Bot) -> None:
    sets = own_sets.get(str(user_id))
    if not sets:
        return await target.answer(warn("Личного набора пока нет, сначала сделай /replace"))
    name = sets[-1]
    try:
        s = await bot.get_sticker_set(name)
    except TelegramAPIError as e:
        return await target.answer(err(f"Не смог открыть набор: {html.escape(str(e))}"))
    await target.answer(
        f"{pe(E_GIFT, '🎁')} <b>Мой набор</b>\n"
        f"Эмодзи в наборе: {len(s.stickers)}\n"
        f"Наборов всего: {len(sets)}",
        reply_markup=open_set_markup(name),
    )


# ============================================================
#  ХЭНДЛЕРЫ
# ============================================================
@router.message(Command("start", "help"))
async def cmd_start(message: Message):
    await send_menu(message, HELP_TEXT, main_menu())


@router.callback_query(F.data.startswith("menu:"))
async def on_menu(callback: CallbackQuery, bot: Bot):
    action = (callback.data or "").split(":", 1)[1]
    await callback.answer()  # всплывающие ответы — без премиум-эмодзи, по гайду
    msg = callback.message
    if not isinstance(msg, Message):
        return
    if action == "load":
        await msg.answer(f"{pe(E_FOLDER, '📂')} Отправь: /load EmojiStriver")
    elif action == "list":
        await show_list(msg, callback.from_user.id)
    elif action == "myset":
        await show_myset(msg, callback.from_user.id, bot)
    elif action == "help":
        await send_menu(msg, HELP_TEXT, main_menu())


@router.message(Command("load"))
async def cmd_load(message: Message, command: CommandObject, bot: Bot):
    user_id = message.from_user.id
    if not command.args:
        return await message.answer(warn("Использование: /load EmojiStriver"))

    name = pack_name_from(command.args)
    if not PACK_NAME_RE.match(name):
        return await message.answer(err("Некорректное имя пака"))

    try:
        pack = await bot.get_sticker_set(name)
    except TelegramAPIError as e:
        return await message.answer(err(f"Не удалось загрузить пак: {html.escape(str(e))}"))

    if pack.sticker_type != "custom_emoji":
        return await message.answer(warn("Это не пак премиум-эмодзи, нужна ссылка вида t.me/addemoji/..."))

    loaded[user_id] = {"pack": name, "stickers": pack.stickers}
    await message.answer(
        ok(f"Пак <b>{html.escape(pack.title)}</b> загружен, эмодзи: {len(pack.stickers)}.\n"
           "Смотри номера через /list")
    )


@router.message(Command("list"))
async def cmd_list(message: Message):
    await show_list(message, message.from_user.id)


@router.message(Command("replace"))
async def cmd_replace(message: Message, command: CommandObject, bot: Bot):
    user_id = message.from_user.id
    data = loaded.get(user_id)
    if not data:
        return await message.answer(err("Сначала загрузи пак: /load EmojiStriver"))

    parts = (command.args or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit():
        return await message.answer(warn("Использование: /replace 3 Привет"))

    idx = int(parts[0])
    new_text = parts[1].strip()
    if not new_text:
        return await message.answer(warn("Текст не может быть пустым"))
    if len(new_text) > MAX_TEXT_LEN:
        return await message.answer(warn(f"Текст длиннее {MAX_TEXT_LEN} символов"))
    if idx >= len(data["stickers"]):
        return await message.answer(err("Нет эмодзи с таким номером, смотри /list"))

    src = data["stickers"][idx]
    if src.is_video or not src.is_animated:
        return await message.answer(err("Текст можно менять только в анимированных TGS-эмодзи"))

    try:
        raw = await download_bytes(bot, src)
        tgs = make_text_tgs(raw, new_text)
        async with lock_for(user_id):
            name = await add_to_own_set(bot, user_id, tgs, src.emoji or "✨")
        custom_id = await last_custom_id(bot, name)
    except EmojiError as e:
        return await message.answer(err(html.escape(str(e))))
    except TelegramAPIError as e:
        log.warning("Telegram отклонил /replace: %s", e)
        return await message.answer(err(f"Telegram отклонил эмодзи: {html.escape(str(e))}"))
    except Exception:
        log.exception("Непредвиденная ошибка в /replace")
        return await message.answer(err("Внутренняя ошибка, попробуй ещё раз"))

    if custom_id is None:
        return await message.answer(err("Эмодзи добавлен, но не удалось получить его ID"))

    await message.answer(
        ok(f"Готово: {pe(custom_id, src.emoji or '✨')}"),
        reply_markup=open_set_markup(name),
    )


@router.message(Command("myset"))
async def cmd_myset(message: Message, bot: Bot):
    await show_myset(message, message.from_user.id, bot)


@router.message(Command("delete"))
async def cmd_delete(message: Message, command: CommandObject, bot: Bot):
    user_id = message.from_user.id
    sets = own_sets.get(str(user_id))
    if not sets:
        return await message.answer(warn("Личного набора пока нет"))

    arg = (command.args or "").strip()
    if not arg.isdigit():
        return await message.answer(warn("Использование: /delete 0"))

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


@router.message(Command("info"))
async def cmd_info(message: Message, command: CommandObject, bot: Bot):
    if not command.args:
        return await message.answer(warn("Использование: /info EmojiStriver"))
    name = pack_name_from(command.args)
    if not PACK_NAME_RE.match(name):
        return await message.answer(err("Некорректное имя пака"))
    try:
        s = await bot.get_sticker_set(name)
    except TelegramAPIError as e:
        return await message.answer(err(f"Не нашёл пак: {html.escape(str(e))}"))
    await message.answer(
        f"{pe(E_INFO, 'ℹ')} <b>{html.escape(s.title)}</b>\n"
        f"Тип: {html.escape(str(s.sticker_type))}\n"
        f"Эмодзи: {len(s.stickers)}"
    )


@router.message(Command("debug"))
async def cmd_debug(message: Message, command: CommandObject, bot: Bot):
    """Показывает, какие слои внутри эмодзи — чтобы понять, почему нет текста."""
    data = loaded.get(message.from_user.id)
    if not data:
        return await message.answer(err("Сначала загрузи пак: /load EmojiStriver"))
    arg = (command.args or "").strip()
    if not arg.isdigit() or int(arg) >= len(data["stickers"]):
        return await message.answer(warn("Использование: /debug 3 (номер из /list)"))

    src = data["stickers"][int(arg)]
    if src.is_video or not src.is_animated:
        return await message.answer(err("Это не TGS-анимация"))
    try:
        raw = await download_bytes(bot, src)
        obj = decode_tgs(raw)
    except EmojiError as e:
        return await message.answer(err(html.escape(str(e))))
    except TelegramAPIError as e:
        return await message.answer(err(f"Не смог скачать эмодзи: {html.escape(str(e))}"))

    counter: Counter = Counter()
    collect_layer_types(obj, counter)
    if not counter:
        return await message.answer(err("Слоёв не нашёл, файл пустой или нестандартный"))
    lines = [
        f"• {LAYER_NAMES.get(ty, 'ty=' + str(ty))} (ty={ty}): {n}"
        for ty, n in sorted(counter.items())
    ]
    await message.answer(
        f"{pe(E_DEBUG, '📁')} <b>Слои эмодзи {arg}</b>\n" + "\n".join(lines)
    )


# ============================================================
#  ЗАПУСК
# ============================================================
async def main() -> None:
    global _bot_username
    if not BOT_TOKEN:
        raise SystemExit("Задай переменную окружения BOT_TOKEN")

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
