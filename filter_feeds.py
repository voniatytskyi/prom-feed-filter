#!/usr/bin/env python3
"""
Фільтр фідів постачальника (EasyDrop, формат Prom XML <shop><catalog><items>).

Що робить:
  1. Завантажує кожен фід із feeds.txt.
  2. Вирізає товари з уцінкою / браком / нюансом / дефектом / розпаровкою, б/у
     та товари 18+:
       - у назві, описі чи будь-якому параметрі (наприклад «Розмір = 41 уцінка»),
       - за службовими кодами постачальника (lot820-…, LOT: 3088-…, Rzp127-…),
       - якщо категорія постачальника називається «Уцінка…», «Брак», «18+» тощо.
     Фрази на кшталт «без потертості», «без браку» НЕ вважаються браком;
     «Unisex/унісекс» не вважається секс-товаром.
     Звичайні розпродажі (SALE, «Розпродаж») НЕ чіпаються.
  3. Записує очищений фід у папку output/ (той самий формат, решта тексту без змін)
     і звіт removed_<name>.csv з переліком вирізаних товарів.

Захист від збоїв: якщо постачальник віддав порожній/битий файл або фільтр
вирізав би підозріло багато товарів, старий очищений фід НЕ перезаписується —
інакше Prom міг би масово зняти товари з продажу.

Запуск:  python filter_feeds.py            (потрібен лише Python 3.8+, без бібліотек)
         python filter_feeds.py --local f.xml   (перевірка на локальному файлі)
"""
import argparse
import csv
import html
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
FEEDS_FILE = BASE / "feeds.txt"
OUT_DIR = BASE / "output"

# --- Правила -----------------------------------------------------------------
# Початок слова (кирилиця/латиниця), щоб не ловити шматки інших слів.
_W = r"(?:^|[^а-яіїєґёa-z])"
BAD_WORDS = re.compile(
    _W + r"(уцін|уцен|брак|дефект|нюанс|некондиц|пошкодж|повреж|розпар|распар|"
    r"б/у|вживан|секонд|секс|sex\b|інтим|интим|еротик|эротик|18\+)",
    re.I,
)
# В описах — лише однозначні слова: фрази на кшталт «приховує дефекти пластику»
# трапляються у звичайних рекламних текстах і не означають брак товару.
BAD_DESC = re.compile(
    _W + r"(уцін|уцен|брак|розпар|распар|б/у|вживан|секонд|секс|sex\b|інтим|интим|еротик|эротик|18\+)",
    re.I,
)
BAD_CATEGORY = re.compile(_W + r"(уцін|уцен|брак|розпар|секс|sex\b|інтим|интим|18\+)", re.I)
# Службові коди постачальника для уцінки та розпарок: «lot820-…», «LOT: 3088-…», «Rzp127-…».
# Слово LOT у назві моделі (Nike x Off-White «LOT 30:50», CLOT, Merlot) НЕ чіпаємо.
BAD_CODE = re.compile(r"^\s*(lot|rzp)\s*:?\s*\d", re.I)       # у коді товару
BAD_CODE_NAME = re.compile(r"^\s*(lot\s*:\s*\d|rzp\s*:?\s*\d)", re.I)  # у назві: «LOT: 3088 …»
NEGATIONS = re.compile(r"без\s+(потертост\w*|дефект\w*|брак\w*|нюанс\w*)", re.I)

# Додаткові правила з файлу exclude_rules.txt (бренди зі скаргами, ознаки копій, конкретні коди)
RULES_FILE = BASE / "exclude_rules.txt"
CODE_TAIL = re.compile(r"(\d+)\s*$")


def load_rules():
    codes, brands, phrases = set(), [], []
    if not RULES_FILE.exists():
        return codes, brands, phrases
    for raw in RULES_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        kind, value = (s.strip() for s in line.split(":", 1))
        if not value:
            continue
        if kind == "code":
            codes.add(value.lower())
        elif kind == "brand":
            brands.append(re.compile(value, re.I))
        elif kind == "phrase":
            phrases.append(re.compile(value, re.I))
    return codes, brands, phrases


EX_CODES, EX_BRANDS, EX_PHRASES = load_rules()


def extra_reason(name: str, code: str, desc: str) -> str:
    tail = CODE_TAIL.search(code)
    if code.lower() in EX_CODES or (tail and tail.group(1) in EX_CODES):
        return f"у списку виключень: {code}"
    for r in EX_BRANDS:
        if r.search(name):
            return f"бренд зі скаргою: {r.search(name).group(0)}"
    for r in EX_PHRASES:
        m = r.search(name) or r.search(desc)
        if m:
            return f"ознака копії: {m.group(0).strip()}"
    return ""

# Запобіжники
MIN_ITEMS = 1000          # менше товарів у фіді — вважаємо, що файл битий
MAX_REMOVED_SHARE = 0.15  # вирізали б більше 15% — щось не так, не публікуємо

ITEM_RE = re.compile(r"<item\b[^>]*>.*?</item>[ \t]*\r?\n?", re.S)
CAT_RE = re.compile(r'<category\s+id="([^"]+)"[^>]*>(.*?)</category>', re.S)
TAG_RE = {t: re.compile(rf"<{t}\b[^>]*>(.*?)</{t}>", re.S) for t in ("name", "categoryId", "vendorCode", "description")}
PARAM_RE = re.compile(r"<param\b[^>]*>(.*?)</param>", re.S)
ATTR_ID = re.compile(r'<item\b[^>]*\bid="([^"]*)"')


def clean(text: str) -> str:
    return NEGATIONS.sub(" ", html.unescape(text or ""))


def first(tag: str, block: str) -> str:
    m = TAG_RE[tag].search(block)
    return html.unescape(m.group(1)).strip() if m else ""


def reason(block: str, bad_cats: dict) -> str:
    """Повертає причину видалення або '' якщо товар залишається."""
    cat_id = first("categoryId", block)
    if cat_id in bad_cats:
        return f"категорія: {bad_cats[cat_id]}"
    name = first("name", block)
    code = first("vendorCode", block)
    if BAD_CODE.search(code) or BAD_CODE_NAME.search(name):
        return f"службовий код: {code}"
    if BAD_WORDS.search(clean(name)):
        return "назва"
    for p in PARAM_RE.findall(block):
        if BAD_WORDS.search(clean(p)):
            return f"параметр: {html.unescape(p).strip()}"
    desc = first("description", block)
    if BAD_DESC.search(clean(desc)):
        return "опис"
    return extra_reason(name, code, html.unescape(desc))


def filter_feed(xml: str):
    cats = {cid: html.unescape(n).strip() for cid, n in CAT_RE.findall(xml)}
    bad_cats = {cid: n for cid, n in cats.items() if BAD_CATEGORY.search(n)}
    removed, total = [], 0

    def repl(m):
        nonlocal total
        total += 1
        block = m.group(0)
        why = reason(block, bad_cats)
        if why:
            idm = ATTR_ID.search(block)
            removed.append({
                "id": idm.group(1) if idm else "",
                "vendorCode": first("vendorCode", block),
                "name": first("name", block),
                "reason": why,
            })
            return ""
        return block

    out = ITEM_RE.sub(repl, xml)
    return drop_empty_categories(out), total, removed


CAT_LINE_RE = re.compile(r'[ \t]*<category\s+id="([^"]+)"([^>]*)>.*?</category>[ \t]*\r?\n?', re.S)
PARENT_RE = re.compile(r'parentId="([^"]+)"')
CATID_RE = re.compile(r"<categoryId>\s*([^<\s]+)\s*</categoryId>")


def drop_empty_categories(xml: str) -> str:
    """Прибирає категорії, у яких не лишилося жодного товару («Уцінка», «Gucci» тощо),
    щоб Prom не створював порожні групи. Батьківські категорії непорожніх груп лишаються."""
    parents = {cid: (PARENT_RE.search(attrs) or [None, None])[1] for cid, attrs in CAT_LINE_RE.findall(xml)}
    keep = set()
    for cid in set(CATID_RE.findall(xml)):
        while cid and cid not in keep:
            keep.add(cid)
            cid = parents.get(cid)
    return CAT_LINE_RE.sub(lambda m: m.group(0) if m.group(1) in keep else "", xml)


def download(url: str, tries: int = 3) -> str:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "feed-filter/1.0"})
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read().decode("utf-8")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(10 * (i + 1))
    raise RuntimeError(f"не вдалося завантажити фід: {last}")


def read_feeds():
    # На GitHub посилання з ключами лежать у секреті FEEDS (той самий формат, що й feeds.txt),
    # щоб вони не потрапили у публічний репозиторій.
    text = os.environ.get("FEEDS") or FEEDS_FILE.read_text(encoding="utf-8")
    feeds = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, url = line.split(None, 1)
        feeds.append((name, url.strip()))
    return feeds


def process(name: str, xml: str) -> bool:
    out, total, removed = filter_feed(xml)
    share = len(removed) / total if total else 1
    print(f"[{name}] товарів: {total}, вирізано: {len(removed)} ({share:.1%})")

    if total < MIN_ITEMS or "</items>" not in out:
        print(f"[{name}] ПОМИЛКА: фід виглядає неповним — старий файл залишено без змін")
        return False
    if share > MAX_REMOVED_SHARE:
        print(f"[{name}] ПОМИЛКА: вирізано понад {MAX_REMOVED_SHARE:.0%} — перевірте правила, старий файл залишено")
        return False

    OUT_DIR.mkdir(exist_ok=True)
    tmp = OUT_DIR / f"{name}.xml.tmp"
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, OUT_DIR / f"{name}.xml")  # атомарна заміна: Prom ніколи не бачить напівзаписаний файл

    with open(OUT_DIR / f"removed_{name}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["id", "vendorCode", "name", "reason"], delimiter=";")
        w.writeheader()
        w.writerows(removed)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", help="перевірити фільтр на локальному XML-файлі")
    ap.add_argument("--min-items", type=int, default=None)
    ap.add_argument("--max-share", type=float, default=None)
    args = ap.parse_args()

    global MIN_ITEMS, MAX_REMOVED_SHARE
    if args.min_items is not None:
        MIN_ITEMS = args.min_items
    if args.max_share is not None:
        MAX_REMOVED_SHARE = args.max_share

    if args.local:
        ok = process(Path(args.local).stem + "_filtered", Path(args.local).read_text(encoding="utf-8"))
        sys.exit(0 if ok else 1)

    ok_all = True
    for name, url in read_feeds():
        try:
            ok_all &= process(name, download(url))
        except Exception as e:  # noqa: BLE001
            print(f"[{name}] ПОМИЛКА: {e} — старий файл залишено")
            ok_all = False
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
