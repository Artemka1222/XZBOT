"""
Показывает структуру TGS-эмодзи: слои, precomp, текстовые слои, имена групп фигур.

Использование:
  python tgs_dump.py файл.tgs
  BOT_TOKEN=... python tgs_dump.py EmojiStriver 1      # номер эмодзи в паке

Зачем: чтобы понять, где в анимации лежит надпись (текстовый слой,
фигуры, или precomp), прежде чем писать замену.
"""

import asyncio
import gzip
import io
import json
import os
import sys

LAYER_NAMES = {0: "precomp", 1: "solid", 2: "image", 3: "null", 4: "shape", 5: "text"}
MAX_LINES = 400


def text_of(layer: dict) -> str | None:
    """Текст текстового слоя (ty = 5), если он есть."""
    d = (layer.get("t") or {}).get("d") or {}
    k = d.get("k")
    if isinstance(k, dict):
        return k.get("t")
    if isinstance(k, list) and k and isinstance(k[0], dict):
        s = k[0].get("s")
        return s.get("t") if isinstance(s, dict) else None
    return None


def describe(obj: dict) -> list[str]:
    lines: list[str] = []
    assets = {a.get("id"): a for a in obj.get("assets", []) if isinstance(a, dict)}

    def walk(layers, depth: int, seen: frozenset) -> None:
        pad = "  " * depth
        for layer in layers:
            if not isinstance(layer, dict):
                continue
            if len(lines) >= MAX_LINES:
                lines.append("... (дальше не показываю)")
                return
            ty = layer.get("ty")
            label = LAYER_NAMES.get(ty, f"ty={ty}")
            extra = ""
            if ty == 5:
                extra = f' TEXT="{text_of(layer)}"'
            elif ty == 4:
                groups = [
                    g.get("nm", "")
                    for g in layer.get("shapes", [])
                    if isinstance(g, dict) and g.get("ty") == "gr"
                ]
                if groups:
                    extra = f" groups={groups[:10]}"
            lines.append(f"{pad}{label} ind={layer.get('ind')} nm={layer.get('nm', '')!r}{extra}")

            ref = layer.get("refId")
            if ref is not None and ref in assets and ref not in seen:
                lines.append(f"{pad}  [precomp {ref}]")
                walk(assets[ref].get("layers", []), depth + 2, seen | {ref})

    walk(obj.get("layers", []), 0, frozenset())
    return lines


def summary(obj: dict) -> str:
    counts: dict[str, int] = {}

    def count(node) -> None:
        if isinstance(node, dict):
            ty = node.get("ty")
            if isinstance(ty, int) and "ind" in node:
                name = LAYER_NAMES.get(ty, f"ty={ty}")
                counts[name] = counts.get(name, 0) + 1
            for v in node.values():
                if isinstance(v, (dict, list)):
                    count(v)
        elif isinstance(node, list):
            for item in node:
                count(item)

    count(obj)
    parts = [f"{k}: {v}" for k, v in sorted(counts.items())]
    return ", ".join(parts) if parts else "слоёв нет"


async def fetch_sticker(pack: str, index: int, token: str) -> bytes:
    from aiogram import Bot

    bot = Bot(token)
    try:
        s = await bot.get_sticker_set(pack)
        if index >= len(s.stickers):
            raise SystemExit(f"В паке {len(s.stickers)} эмодзи, номер {index} не существует")
        buf = io.BytesIO()
        await bot.download(s.stickers[index], destination=buf)
        return buf.getvalue()
    finally:
        await bot.session.close()


def main() -> None:
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)

    if args[0].endswith(".tgs") or os.path.isfile(args[0]):
        with open(args[0], "rb") as f:
            raw = f.read()
    else:
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("Задай BOT_TOKEN для загрузки из пака")
        if len(args) < 2 or not args[1].isdigit():
            raise SystemExit("Использование: python tgs_dump.py EmojiStriver 1")
        raw = asyncio.run(fetch_sticker(args[0], int(args[1]), token))

    obj = json.loads(gzip.decompress(raw))
    print("Итого:", summary(obj))
    print("-" * 40)
    for line in describe(obj):
        print(line)


if __name__ == "__main__":
    main()
