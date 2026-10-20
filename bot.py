import asyncio
import gzip
import html
import io
import json
import logging
import os
import secrets

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputSticker,
    Message,
)

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise SystemExit("Задай переменную окружения BOT_TOKEN")

# file_id GIF/анимации для меню (необязательно). Если пусто, меню отправляется текстом.
MENU_GIF_ID = os.getenv("MENU_GIF_ID", "")

MAX_TEXT_LEN = 20
router = Router()

# user_id -> {"pack": str, "stickers": list[Sticker]}
loaded: dict[int, dict] = {}
# user_id -> имя личного набора, созданного ботом
own_sets: dict[int, str] = {}

# ---------- Премиум-эмодзи (ID из emoji.wivvi.net/llms.txt) ----------
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


def pe(emoji_id: str, fallback: str) -> str:
    """Премиум-эмодзи для текста. fallback — обычный эмодзи из списка."""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def ok(text: str) -> str:
    return f"{pe(E_OK, '✅')} {text}"


def err(text: str) -> str:
    return f"{pe(E_ERR, '❌')} {text}"


def warn(text: str) -> str:
    return f"{pe(E_WARN, '❗️')} {text}"


def btn(text: str, emoji_id: str, callback_data: str | None = None, url: str | None = None):
    """Кнопка с премиум-иконкой. Текст без обычного эмодзи."""
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
    f"{pe(E_INFO, 'ℹ')} /info &lt;пак&gt; — информация о паке"
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


async def send_menu(target: Message, text: str, markup: InlineKeyboardMarkup):
    """Меню с GIF, если задан MENU_GIF_ID, иначе обычным текстом."""
    if MENU_GIF_ID:
        return await target.answer_animation(MENU_GIF_ID, caption=text, reply_markup=markup)
    return await target.answer(text, reply_markup=markup)


def pack_name_from(arg: str) -> str:
    """Принимает 'EmojiStriver' или 'https://t.me/addemoji/EmojiStriver'."""
    return arg.strip().rstrip("/").split("/")[-1]


def replace_text_in_node(node, text: str) -> int:
    """Рекурсивно находит текстовые слои Lottie (ty == 5) и меняет строку текста."""
    count = 0
    if isinstance(node, dict):
        if node.get("ty") == 5 and isinstance(node.get("t"), dict):
            count += _set_text_docs(node["t"], text)
        for value in node.values():
            if isinstance(value, (dict, list)):
                count += replace_text_in_node(value, text)
    elif isinstance(node, list):
        for item in node:
            count += replace_text_in_node(item, text)
    return count


def _set_text_docs(t_obj: dict, text: str) -> int:
    d = t_obj.get("d")
    if not isinstance(d, dict):
        return 0
    k = d.get("k")
    frames = k if isinstance(k, list) else [k]
    n = 0
    for frame in frames:
        if isinstance(frame, dict) and isinstance(frame.get("s"), dict) and "t" in frame["s"]:
            frame["s"]["t"] = text
            n += 1
    return n


async def download_bytes(bot: Bot, sticker) -> bytes:
    buf = io.BytesIO()
    await bot.download(sticker, destination=buf)
    return buf.getvalue()


async def show_list(target: Message, user_id: int):
    data = loaded.get(user_id)
    if not data:
        return await target.answer(err("Сначала загрузи пак: /load EmojiStriver"))
    rows = []
    for i, st in enumerate(data["stickers"][:60]):
        rows.append(f"{i}. {pe(st.custom_emoji_id, st.emoji or '•')}")
    extra = len(data["stickers"]) - 60
    text = f"{pe(E_LIST, '🗂')} <b>Эмодзи пака</b>\n" + "\n".join(rows)
    if extra > 0:
        text += f"\n… и ещё {extra}"
    await target.answer(text)


async def show_myset(target: Message, user_id: int, bot: Bot):
    name = own_sets.get(user_id)
    if not name:
        return await target.answer(warn("Личного набора пока нет, сначала сделай /replace"))
    s = await bot.get_sticker_set(name)
    await target.answer(
        f"{pe(E_GIFT, '🎁')} <b>Мой набор</b>\nЭмодзи: {len(s.stickers)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [btn("Открыть набор", E_FOLDER, url=f"https://t.me/addemoji/{name}")]
        ]),
    )


@router.message(Command("start", "help"))
async def cmd_start(message: Message):
    await send_menu(message, HELP_TEXT, main_menu())


@router.callback_query(F.data.startswith("menu:"))
async def on_menu(callback: CallbackQuery, bot: Bot):
    action = callback.data.split(":", 1)[1]
    await callback.answer()  # всплывающий текст без премиум-эмодзи
    msg = callback.message
    if msg is None:
        return
    if action == "load":
        await msg.answer(f"{pe(E_FOLDER, '📂')} Отправь команду: /load EmojiStriver")
    elif action == "list":
        await show_list(msg, callback.from_user.id)
    elif action == "myset":
        await show_myset(msg, callback.from_user.id, bot)
    elif action == "help":
        await send_menu(msg, HELP_TEXT, main_menu())


@router.message(Command("load"))
async def cmd_load(message: Message, command: CommandObject, bot: Bot):
    if not command.args:
        return await message.answer(err("Использование: /load EmojiStriver"))
    name = pack_name_from(command.args)
    try:
        pack = await bot.get_sticker_set(name)
    except TelegramBadRequest as e:
        return await message.answer(err(f"Не удалось загрузить пак: {html.escape(str(e))}"))

    loaded[message.from_user.id] = {"pack": name, "stickers": pack.stickers}
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
    idx, new_text = int(parts[0]), parts[1].strip()

    if len(new_text) > MAX_TEXT_LEN:
        return await message.answer(warn(f"Текст длиннее {MAX_TEXT_LEN} символов"))
    if idx >= len(data["stickers"]):
        return await message.answer(err("Нет эмодзи с таким номером"))

    src = data["stickers"][idx]
    if not src.is_animated:
        return await message.answer(err("Этот эмодзи не анимированный, текст через TGS не поменять"))

    raw = await download_bytes(bot, src)
    try:
        obj = json.loads(gzip.decompress(raw))
    except (OSError, json.JSONDecodeError):
        return await message.answer(err("Не смог прочитать TGS-файл эмодзи"))

    changed = replace_text_in_node(obj, new_text)
    if changed == 0:
        return await message.answer(err("В этом эмодзи не нашёл текстовых слоёв"))

    new_tgs = gzip.compress(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode())
    sticker = InputSticker(
        sticker=BufferedInputFile(new_tgs, filename="emoji.tgs"),
        format="animated",
        emoji_list=[src.emoji or "✨"],
    )

    me = await bot.get_me()
    name = own_sets.get(user_id)
    try:
        if name is None:
            name = f"t{user_id}_{secrets.token_hex(3)}_by_{me.username}"
            await bot.create_new_sticker_set(
                user_id=user_id,
                name=name,
                title="My text emoji",
                stickers=[sticker],
                sticker_type="custom_emoji",
            )
            own_sets[user_id] = name
        else:
            await bot.add_sticker_to_set(user_id=user_id, name=name, sticker=sticker)
    except TelegramBadRequest as e:
        return await message.answer(err(f"Telegram отклонил набор: {html.escape(str(e))}"))

    created = await bot.get_sticker_set(name)
    custom_id = created.stickers[-1].custom_emoji_id
    await message.answer(
        ok(f"Готово: {pe(custom_id, src.emoji or '✨')}"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [btn("Открыть набор", E_FOLDER, url=f"https://t.me/addemoji/{name}")]
        ]),
    )


@router.message(Command("myset"))
async def cmd_myset(message: Message, bot: Bot):
    await show_myset(message, message.from_user.id, bot)


@router.message(Command("delete"))
async def cmd_delete(message: Message, command: CommandObject, bot: Bot):
    name = own_sets.get(message.from_user.id)
    if not name:
        return await message.answer(warn("Личного набора пока нет"))
    if not command.args or not command.args.strip().isdigit():
        return await message.answer(warn("Использование: /delete 0"))
    s = await bot.get_sticker_set(name)
    idx = int(command.args.strip())
    if idx >= len(s.stickers):
        return await message.answer(err("Нет эмодзи с таким номером"))
    await bot.delete_sticker_from_set(s.stickers[idx].file_id)
    await message.answer(ok("Удалил"))


@router.message(Command("ids"))
async def cmd_ids(message: Message):
    target = message.reply_to_message
    if not target:
        return await message.answer(warn("Ответь командой /ids на сообщение с эмодзи"))
    entities = (target.entities or []) + (target.caption_entities or [])
    ids = [e.custom_emoji_id for e in entities if e.type == "custom_emoji"]
    if not ids:
        return await message.answer(err("В сообщении нет премиум-эмодзи"))
    await message.answer(f"{pe(E_CODE, '🔨')} <b>custom_emoji_id:</b>\n" + "\n".join(ids))


@router.message(Command("info"))
async def cmd_info(message: Message, command: CommandObject, bot: Bot):
    if not command.args:
        return await message.answer(warn("Использование: /info EmojiStriver"))
    try:
        s = await bot.get_sticker_set(pack_name_from(command.args))
    except TelegramBadRequest as e:
        return await message.answer(err(f"Не нашёл пак: {html.escape(str(e))}"))
    await message.answer(
        f"{pe(E_INFO, 'ℹ')} <b>{html.escape(s.title)}</b>\n"
        f"Тип: {s.sticker_type}\n"
        f"Эмодзи: {len(s.stickers)}"
    )


async def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
