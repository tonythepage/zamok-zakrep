"""Синхронизация оглавления с каналом «Егор Строит Замок».

Бот (админ канала) получает новые и отредактированные посты через getUpdates.
Если в посте есть хэштег раздела, пост попадает в оглавление (data.js):

  #эфир        -> Библиотека / Эфиры
  #подкаст     -> Библиотека / Подкасты
  #текст       -> Библиотека / Тексты
  #закрытые    -> Библиотека / Закрытые каналы
  + #луксмаксинг (вместе с #эфир) -> подраздел «Луксмаксинг» во вкладке Эфиры

Название = первая строка поста (без хэштегов). Отредактировал пост -> название обновится.
Убрал хэштег при редактировании -> пост уберётся из оглавления.

Запуск: TG_BOT_TOKEN=... python scripts/sync.py
Для теста без Telegram: FAKE_UPDATES=путь_к.json python scripts/sync.py
"""
import json, os, re, sys, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data.js")
STATE = os.path.join(ROOT, "scripts", "state.json")
PREFIX = "window.LIBRARY="
MSK = timezone(timedelta(hours=3))

TAGS = {  # хэштег (в нижнем регистре, ё->е) -> ключ вкладки
    "эфир": "efiry", "эфиры": "efiry",
    "подкаст": "podcasts", "подкасты": "podcasts",
    "текст": "texts", "тексты": "texts",
    "закрытые": "closed", "закрытыеканалы": "closed",
}
SUBGROUP_TAGS = {"луксмаксинг": "Луксмаксинг"}
HASHTAG = re.compile(r"#([\wёЁ]+)", re.U)


def norm(s):
    return s.lower().replace("ё", "е")


def load_library():
    s = open(DATA, encoding="utf-8").read().strip()
    assert s.startswith(PREFIX)
    return json.loads(s[len(PREFIX):].rstrip(";"))


def save_library(lib):
    with open(DATA, "w", encoding="utf-8") as f:
        f.write(PREFIX + json.dumps(lib, ensure_ascii=False, indent=1) + ";\n")


def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except FileNotFoundError:
        return {"offset": 0}


def get_updates(offset):
    fake = os.environ.get("FAKE_UPDATES")
    if fake:
        return json.load(open(fake, encoding="utf-8"))
    token = os.environ["TG_BOT_TOKEN"]
    q = urllib.parse.urlencode({
        "offset": offset, "timeout": 0, "limit": 100,
        "allowed_updates": json.dumps(["channel_post", "edited_channel_post"]),
    })
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates?{q}", timeout=30) as r:
        data = json.load(r)
    if not data.get("ok"):
        sys.exit(f"Telegram error: {data}")
    return data["result"]


def post_url(chat, mid):
    cid = str(chat["id"])
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{mid}"
    return f"https://t.me/{chat.get('username')}/{mid}"


def parse_post(msg):
    text = msg.get("text") or msg.get("caption") or ""
    tags = [norm(t) for t in HASHTAG.findall(text)]
    tab = next((TAGS[t] for t in tags if t in TAGS), None)
    sub = next((SUBGROUP_TAGS[t] for t in tags if t in SUBGROUP_TAGS), None)
    title = ""
    for line in text.splitlines():
        line = HASHTAG.sub("", line).strip(" \t-—–|·:")
        line = re.sub(r"^[^\w«\"(]+", "", line).strip()  # убрать эмодзи/символы в начале
        if line:
            title = line
            break
    if len(title) > 90:
        title = title[:87].rstrip() + "…"
    dt = datetime.fromtimestamp(msg["date"], MSK)
    return tab, sub if tab == "efiry" else None, title, dt


def find_item(lib, url, auto_only=False):
    for tab in lib:
        for g in tab["groups"]:
            for it in g["items"]:
                if it["u"] == url and (it.get("auto") or not auto_only):
                    return it
    return None


def remove_url(lib, url):
    removed = False
    for tab in lib:
        for g in tab["groups"]:
            n = len(g["items"])
            g["items"] = [it for it in g["items"] if it["u"] != url]
            removed |= len(g["items"]) != n
        tab["groups"] = [g for g in tab["groups"] if g["items"] or g["title"] is None]
    return removed


def upsert(lib, tab_key, sub, item):
    tab = next(t for t in lib if t["key"] == tab_key)
    gtitle = sub
    group = next((g for g in tab["groups"] if g["title"] == gtitle), None)
    if group is None:
        group = {"title": gtitle, "items": []}
        tab["groups"].append(group) if gtitle else tab["groups"].insert(0, group)
    group["items"].insert(0, item)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    lib = load_library()
    state = load_state()
    updates = get_updates(state["offset"])
    changes = []
    for u in updates:
        state["offset"] = max(state["offset"], u["update_id"] + 1)
        msg = u.get("channel_post") or u.get("edited_channel_post")
        if not msg:
            continue
        url = post_url(msg["chat"], msg["message_id"])
        tab, sub, title, dt = parse_post(msg)
        if not tab or not title:
            # без хэштега убираем только то, что добавил сам бот; ручное оглавление не трогаем
            if find_item(lib, url, auto_only=True) and remove_url(lib, url):
                changes.append(f"- убрано: {url}")
            continue
        existed = remove_url(lib, url)
        upsert(lib, tab, sub, {
            "t": title, "d": dt.strftime("%d.%m.%y"), "iso": dt.strftime("%Y-%m-%d"),
            "u": url, "auto": True,
        })
        changes.append(f"{'~ обновлено' if existed else '+ добавлено'} [{tab}{'/' + sub if sub else ''}] {title} — {url}")
    save_library(lib)
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(state, open(STATE, "w", encoding="utf-8"))
    print(f"updates: {len(updates)}, offset: {state['offset']}")
    print("\n".join(changes) or "оглавление без изменений")


if __name__ == "__main__":
    main()
