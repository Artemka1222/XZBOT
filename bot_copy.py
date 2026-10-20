"""
Telegram-бот: копирует пак премиум-эмодзи и меняет текст внутри копии.

Сценарий:
  1. /load https://t.me/addemoji/EmojiStriver   — загрузить пак
  2. /scan                                      — найти эмодзи, где есть текст
  3. /copy                                      — сделать свою копию пака
  4. /replace 3 Привет                          — поменять текст в эмодзи №3 копии

Ограничения, о которых важно знать:
  * Текст меняется только там, где он хранится как текстовый слой Lottie (ty = 5).
    Если текст нарисован фигурами, бот скажет об этом в /scan и /replace.
  * Редактирование эмодзи в Telegram делается через удаление старого и добавление
    нового. Поэтому отредактированное эмодзи переходит в конец набора.
    Бот пишет его новый номер.
  * Лимиты Telegram на количество эмодзи в наборе бот не проверяет заранее:
    если набор заполнен, Telegram вернёт ошибку и бот её покажет.

Требования:
  pip install "aiogram>=3.31"
  BOT_TOKEN=... python bot_copy.py
"""

import asyncio
import gzip
import html
import io
import json
import logging
import os
import re
import secrets

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputSticker, Message

# ============================================================
#  НАСТРОЙКИ
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
STORE_FILE = os.getenv("STORE_FILE", "store.json").strip()

MAX_TEXT_LEN = 20
MAX_TGS_BYTES = 64 * 1024        # лимит Telegram на TGS-эмодзи
NEEDS_REPAINTING = False         # True — эмодзи перекрасятся под цвет текста
FIRST_BATCH = 50                 # сколько эмодзи можно передать при создании набора
DOWNLOAD_CONCURRENCY = 8         # одновременные скачивания
SCAN_LIMIT = 200                 # максимум эмодзи, которые проверяет /scan
PACK_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

LOTTIE_TEXT_LAYER = 5            # ty = 5 — текстовый слой Lottie
LAYER_NAMES = {0: "precomp", 1: "solid", 2: "image", 3: "null", 4: "shape", 5: "text"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("copy_bot")

router = Router()

# user_id (int) -> {"pack": str, "title": str, "count": int}
loaded: dict[int, dict] = {}
# str(user_id) -> имя копии пака (последний созданный набор)
own_copies: dict[str, str] = {}
# один пользователь — одна операция записи за раз
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
    global own_copies
    try:
        with open(STORE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        own_copies = {str(k): str(v) for k, v in data.get("own_copies", {}).items()}
        log.info("Загружено копий: %d", len(own_copies))
    except FileNotFoundError:
        own_copies = {}
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as e:
        log.warning("Не смог прочитать %s (%s), начинаю с пустого", STORE_FILE, e)
        own_copies = {}


def save_store() -> None:
    tmp = STORE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"own_copies": own_copies}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE_FILE)


# ============================================================
#  ОФОРМЛЕНИЕ (премиум-эмодзи из emoji.wivvi.net/llms.txt)
# ============================================================
E_COMMANDS = "5962952497197748583"  # 🔧 Команды /
E_FOLDER = "6039630677182254664"    # 📂 Папка
E_LIST = "5766994197705921104"      # 🗂 Список
E_EDIT = "6039779802741739617"      # ✏️ Редактировать
E_GIFT = "6032644646587338669"      # 🎁 Подарок
E_CODE = "5940433880585605708"      # 🔨 Код </>
E_INFO = "6028435952299413210"      # ℹ Инфо
E_OK = "5774022692642492953"        # ✅ Галочка
E_ERR = "5774077015388852135"       # ❌ Крестик
E_WARN = "6030563507299160824"      # ❗️ Внимание


def pe(emoji_id: str | None, fallback: str) -> str:
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


def btn(text: str, emoji_id: str | None = None, url: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, icon_custom_emoji_id=emoji_id, url=url)


HELP_TEXT = (
    f"{pe(E_COMMANDS, '🔧')} <b>Копия пака с твоим текстом</b>\n"
    f"{pe(E_FOLDER, '📂')} /load &lt;ссылка&gt; — загрузить пак\n"
    f"{pe(E_INFO, 'ℹ')} /scan — где в паке есть текст\n"
    f"{pe(E_GIFT, '🎁')} /copy — сделать мою копию пака\n"
    f"{pe(E_LIST, '🗂')} /list — эмодзи моей копии\n"
    f"{pe(E_EDIT, '✏️')} /replace &lt;номер&gt; &lt;текст&gt; — поменять текст в копии\n"
    f"{pe(E_CODE, '🔨')} /ids — ответом на сообщение показать ID эмодзи"
)


def open_set_markup(name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("Открыть мою копию", E_FOLDER, url=f"https://t.me/addemoji/{name}")]
    ])


# ============================================================
#  РАБОТА С TGS (Lottie)
# ============================================================
class EmojiError(Exception):
    """Понятная пользователю ошибка."""


def decode_tgs(raw: bytes) -> dict:
    try:
        return json.loads(gzip.decompress(raw))
    except (OSError, EOFError, ValueError) as e:
        raise EmojiError(f"Не смог прочитать TGS ({e.__class__.__name__})")


def _set_text(doc, text: str) -> int:
    if isinstance(doc, dict) and isinstance(doc.get("t"), str):
        doc["t"] = text
        return 1
    return 0


def replace_text_in_lottie(node, text: str) -> int:
    """
    Меняет текст во всех текстовых слоях (ty = 5), включая вложенные precomp.
    Текст лежит в layer["t"]["d"]["k"]:
      - статичный текст: k — сам TextDocument с полем "t";
      - ключевые кадры: k — список, TextDocument лежит в "s" каждого кадра.
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


def count_layer_types(node, counter: dict) -> None:
    if isinstance(node, dict):
        ty = node.get("ty")
        if isinstance(ty, int) and "ind" in node:
            counter[ty] = counter.get(ty, 0) + 1
        for value in node.values():
            if isinstance(value, (dict, list)):
                count_layer_types(value, counter)
    elif isinstance(node, list):
        for item in node:
            count_layer_types(item, counter)


def make_text_tgs(raw: bytes, text: str) -> bytes:
    """Возвращает новый TGS с заменённым текстом. Бросает EmojiError, если не получилось."""
    obj = decode_tgs(raw)
    if replace_text_in_lottie(obj, text) == 0:
        raise EmojiError("В этом эмодзи нет текстовых слоёв (текст нарисован фигурами)")
    out = gzip.compress(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if len(out) > MAX_TGS_BYTES:
        raise EmojiError(f"Получился TGS {len(out) // 1024} КБ, лимит 64 КБ. Возьми текст короче")
    return out


def has_text_layers(raw: bytes) -> bool:
    obj = decode_tgs(raw)
    counter: dict = {}
    count_layer_types(obj, counter)
    return counter.get(LOTTIE_TEXT_LAYER, 0) > 0


# ============================================================
#  TELEGRAM: ЗАГРУЗКА, КОПИЯ, ЗАМЕНА
# ============================================================
async def bot_username(bot: Bot) -> str:
    global _bot_username
    if _bot_username is None:
        me = await bot.get_me()
        _bot_username = me.username or ""
    return _bot_username


async def retry_call(make_coro, attempts: int = 5):
    """Повторяет запрос при флуд-лимите Telegram (TelegramRetryAfter)."""
    for i in range(attempts):
        try:
            return await make_coro()
        except TelegramRetryAfter as e:
            wait = float(e.retry_after) + 1
            log.warning("Флуд-лимит, жду %.0f сек (попытка %d)", wait, i + 1)
            await asyncio.sleep(wait)
    raise EmojiError("Telegram не отвечает из-за лимита запросов, попробуй позже")


async def download_all(bot: Bot, stickers) -> list[bytes]:
    sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

    async def one(sticker) -> bytes:
        async with sem:
            buf = io.BytesIO()
            await retry_call(lambda: bot.download(sticker, destination=buf))
            return buf.getvalue()

    return await asyncio.gather(*(one(s) for s in stickers))


def make_input(tgs: bytes, emoji: str) -> InputSticker:
    return InputSticker(
        sticker=BufferedInputFile(tgs, filename="emoji.tgs"),
        format="animated",
        emoji_list=[emoji or "✨"],
    )


async def copy_pack(bot: Bot, user_id: int, pack) -> tuple[str, int]:
    """
    Создаёт копию пака в наборе пользователя. Возвращает (имя набора, сколько эмодзи добавлено).
    """
    stickers = list(pack.stickers)
    raws = await download_all(bot, stickers)

    username = await bot_username(bot)
    name = f"c{user_id}_{secrets.token_hex(3)}_by_{username}"
    title = (f"{pack.title} copy")[:64]

    first = [make_input(raws[i], stickers[i].emoji) for i in range(min(FIRST_BATCH, len(stickers)))]
    await retry_call(lambda: bot.create_new_sticker_set(
        user_id=user_id,
        name=name,
        title=title,
        stickers=first,
        sticker_type="custom_emoji",
        needs_repainting=NEEDS_REPAINTING,
    ))
    added = len(first)

    for i in range(len(first), len(stickers)):
        try:
            await retry_call(lambda i=i: bot.add_sticker_to_set(
                user_id=user_id, name=name, sticker=make_input(raws[i], stickers[i].emoji)
            ))
            added += 1
        except TelegramAPIError as e:
            log.warning("Остановился на эмодзи %d: %s", i, e)
            break

    own_copies[str(user_id)] = name
    save_store()
    return name, added


async def last_custom_id(bot: Bot, name: str) -> str | None:
    s = await bot.get_sticker_set(name)
    return s.stickers[-1].custom_emoji_id if s.stickers else None


# ============================================================
#  ХЭНДЛЕРЫ
# ============================================================
@router.message(Command("start", "help"))
async def cmd_start(message: Message):
    await message.answer(HELP_TEXT)


@router.message(Command("load"))
async def cmd_load(message: Message, command: CommandObject, bot: Bot):
    user_id = message.from_user.id
    if not command.args:
        return await message.answer(warn("Использование: /load https://t.me/addemoji/EmojiStriver"))

    name = command.args.strip().split("?")[0].rstrip("/").split("/")[-1]
    if not PACK_NAME_RE.match(name):
        return await message.answer(err("Некорректное имя пака"))

    try:
        pack = await retry_call(lambda: bot.get_sticker_set(name))
    except TelegramAPIError as e:
        return await message.answer(err(f"Не удалось загрузить пак: {html.escape(str(e))}"))

    if pack.sticker_type != "custom_emoji":
        return await message.answer(warn("Это не пак премиум-эмодзи"))

    loaded[user_id] = {"pack": name, "title": pack.title, "count": len(pack.stickers), "obj": pack}
    await message.answer(
        ok(f"Пак <b>{html.escape(pack.title)}</b> загружен, эмодзи: {len(pack.stickers)}.\n"
           "Дальше: /scan — где есть текст, потом /copy")
    )


@router.message(Command("scan"))
async def cmd_scan(message: Message, bot: Bot):
    data = loaded.get(message.from_user.id)
    if not data:
        return await message.answer(err("Сначала загрузи пак: /load ссылка"))

    pack = data["obj"]
    stickers = list(pack.stickers)[:SCAN_LIMIT]
    status = await message.answer("Проверяю эмодзи…")

    try:
        raws = await download_all(bot, stickers)
    except TelegramAPIError as e:
        return await status.edit_text(err(f"Не смог скачать пак: {html.escape(str(e))}"))

    with_text, without_text, broken = [], [], []
    for i, raw in enumerate(raws):
        if stickers[i].is_video or not stickers[i].is_animated:
            broken.append(i)
            continue
        try:
            (with_text if has_text_layers(raw) else without_text).append(i)
        except EmojiError:
            broken.append(i)

    lines = [f"{pe(E_INFO, 'ℹ')} <b>Проверено эмодзи: {len(stickers)}</b>"]
    lines.append(f"Текстовые слои есть: {', '.join(map(str, with_text)) or 'нет'}")
    lines.append(f"Текстовых слоёв нет: {len(without_text)}")
    if broken:
        lines.append(f"Не TGS-анимация: {len(broken)}")
    if not with_text:
        lines.append(warn("Ни в одном эмодзи нет текстовых слоёв, менять текст в этом паке нельзя"))
    await status.edit_text("\n".join(lines))


@router.message(Command("copy"))
async def cmd_copy(message: Message, bot: Bot):
    user_id = message.from_user.id
    data = loaded.get(user_id)
    if not data:
        return await message.answer(err("Сначала загрузи пак: /load ссылка"))

    status = await message.answer("Делаю копию пака, это может занять пару минут…")
    try:
        async with lock_for(user_id):
            name, added = await copy_pack(bot, user_id, data["obj"])
    except EmojiError as e:
        return await status.edit_text(err(html.escape(str(e))))
    except TelegramAPIError as e:
        log.warning("Ошибка копии: %s", e)
        return await status.edit_text(err(f"Telegram отклонил копию: {html.escape(str(e))}"))
    except Exception:
        log.exception("Непредвиденная ошибка в /copy")
        return await status.edit_text(err("Внутренняя ошибка, попробуй ещё раз"))

    total = data["count"]
    note = "" if added == total else f"\n{warn(f'Скопировано {added} из {total}, дальше Telegram не дал добавить')}"
    await status.edit_text(
        ok(f"Копия готова: {added} эмодзи.") + note,
        reply_markup=open_set_markup(name),
    )


@router.message(Command("list"))
async def cmd_list(message: Message, bot: Bot):
    name = own_copies.get(str(message.from_user.id))
    if not name:
        return await message.answer(warn("Копии пока нет, сделай /copy"))
    try:
        s = await bot.get_sticker_set(name)
    except TelegramAPIError as e:
        return await message.answer(err(f"Не смог открыть копию: {html.escape(str(e))}"))

    rows = [f"{i}. {pe(st.custom_emoji_id, st.emoji or '•')}" for i, st in enumerate(s.stickers[:60])]
    text = f"{pe(E_LIST, '🗂')} <b>Моя копия</b> ({len(s.stickers)})\n" + "\n".join(rows)
    if len(s.stickers) > 60:
        text += f"\n… и ещё {len(s.stickers) - 60}"
    await message.answer(text, reply_markup=open_set_markup(name))


@router.message(Command("replace"))
async def cmd_replace(message: Message, command: CommandObject, bot: Bot):
    user_id = message.from_user.id
    name = own_copies.get(str(user_id))
    if not name:
        return await message.answer(warn("Сначала сделай копию: /copy"))

    parts = (command.args or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit():
        return await message.answer(warn("Использование: /replace 3 Привет"))

    idx = int(parts[0])
    text = " ".join(parts[1].split())
    if not text:
        return await message.answer(warn("Текст не может быть пустым"))
    if len(text) > MAX_TEXT_LEN:
        return await message.answer(warn(f"Текст длиннее {MAX_TEXT_LEN} символов"))

    try:
        s = await bot.get_sticker_set(name)
    except TelegramAPIError as e:
        return await message.answer(err(f"Не смог открыть копию: {html.escape(str(e))}"))
    if idx >= len(s.stickers):
        return await message.answer(err("Нет эмодзи с таким номером, смотри /list"))

    src = s.stickers[idx]
    if src.is_video or not src.is_animated:
        return await message.answer(err("Это не TGS-анимация, текст поменять нельзя"))

    try:
        async with lock_for(user_id):
            raw = (await download_all(bot, [src]))[0]
            new_tgs = make_text_tgs(raw, text)
            # Сначала добавляем новое, потом удаляем старое: набор никогда не остаётся пустым
            await retry_call(lambda: bot.add_sticker_to_set(
                user_id=user_id, name=name, sticker=make_input(new_tgs, src.emoji)
            ))
            await retry_call(lambda: bot.delete_sticker_from_set(src.file_id))
    except EmojiError as e:
        return await message.answer(err(html.escape(str(e))))
    except TelegramAPIError as e:
        log.warning("Telegram отклонил замену: %s", e)
        return await message.answer(err(f"Telegram отклонил замену: {html.escape(str(e))}"))
    except Exception:
        log.exception("Непредвиденная ошибка в /replace")
        return await message.answer(err("Внутренняя ошибка, попробуй ещё раз"))

    s = await bot.get_sticker_set(name)
    new_idx = len(s.stickers) - 1
    new_id = s.stickers[new_idx].custom_emoji_id
    await message.answer(
        ok(f"Текст заменён: {pe(new_id, src.emoji or '✨')}\n"
           f"Эмодзи переехал в конец набора, теперь его номер <b>{new_idx}</b>"),
        reply_markup=open_set_markup(name),
    )


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
