"""Синхронизация оглавления с каналом «Егор Строит Замок».

Запускается GitHub Actions по расписанию. Бот (админ канала) забирает обновления через getUpdates.

1) Модерация кнопками. Каждый новый пост канала бот присылает модераторам (scripts/config.json)
   с кнопками разделов: Эфир / Подкаст / Текст / Закрытые / Эфир·Луксмаксинг / Пропустить.
   Нажал раздел -> пост попадает в оглавление. Можно «Убрать» обратно или «Всё-таки добавить» после пропуска.
   Ответ (reply) на сообщение бота -> своё название для поста в оглавлении.
2) Хэштеги (по желанию): #эфир #подкаст #текст #закрытые (+ #луксмаксинг) -> добавляется сразу.

Запуск: TG_BOT_TOKEN=... python scripts/sync.py
Тест без Telegram: FAKE_UPDATES=updates.json python scripts/sync.py (вызовы API печатаются, не отправляются)
"""
import html, json, os, re, sys, time, urllib.request
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data.js")
STATE = os.path.join(ROOT, "scripts", "state.json")
CONFIG = os.path.join(ROOT, "scripts", "config.json")
PREFIX = "window.LIBRARY="
MSK = timezone(timedelta(hours=3))
FAKE = os.environ.get("FAKE_UPDATES")

TAGS = {
    "эфир": "efiry", "эфиры": "efiry", "подкаст": "podcasts", "подкасты": "podcasts",
    "текст": "texts", "тексты": "texts", "закрытые": "closed", "закрытыеканалы": "closed",
}
SECTIONS = {  # код кнопки -> (вкладка, подраздел, подпись)
    "efiry": ("efiry", None, "Эфиры"),
    "lx": ("efiry", "Луксмаксинг", "Эфиры · Луксмаксинг"),
    "podcasts": ("podcasts", None, "Подкасты"),
    "texts": ("texts", None, "Тексты"),
    "closed": ("closed", None, "Закрытые каналы"),
}
HASHTAG = re.compile(r"#([\wёЁ]+)", re.U)
MEDIA = ("video", "audio", "voice", "photo", "document", "video_note", "animation", "poll")
PENDING_TTL = 30 * 86400


# ---------------- файлы ----------------
def load_library():
    s = open(DATA, encoding="utf-8").read().strip()
    return json.loads(s[len(PREFIX):].rstrip(";"))


def save_library(lib):
    with open(DATA, "w", encoding="utf-8") as f:
        f.write(PREFIX + json.dumps(lib, ensure_ascii=False, indent=1) + ";\n")


def load_json(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except FileNotFoundError:
        return default


# ---------------- Telegram API ----------------
def api(method, **params):
    if FAKE:
        print(f"  [api] {method} {json.dumps(params, ensure_ascii=False)[:300]}")
        return {"message_id": 5000 + len(params.get("text", ""))} if method == "sendMessage" else True
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{os.environ['TG_BOT_TOKEN']}/{method}",
        data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r).get("result")
    except urllib.error.HTTPError as e:
        print(f"  [api] {method} failed: {e.code} {e.read()[:200]!r}")
        return None


def get_updates(offset):
    if FAKE:
        return json.load(open(FAKE, encoding="utf-8"))
    res = api("getUpdates", offset=offset, timeout=0, limit=100,
              allowed_updates=["channel_post", "edited_channel_post", "message", "callback_query"])
    if res is None:
        sys.exit("getUpdates failed (неверный токен или у бота включён webhook)")
    return res


# ---------------- разбор постов ----------------
def norm(s):
    return s.lower().replace("ё", "е")


def post_url(chat, mid):
    cid = str(chat["id"])
    return f"https://t.me/c/{cid[4:]}/{mid}" if cid.startswith("-100") else f"https://t.me/{chat.get('username')}/{mid}"


def clean_title(text):
    for line in (text or "").splitlines():
        line = HASHTAG.sub("", line).strip(" \t-—–|·:")
        line = re.sub(r"^[^\w«\"(]+", "", line).strip()
        if line:
            return line[:87].rstrip() + "…" if len(line) > 90 else line
    return ""


def tag_section(text):
    tags = [norm(t) for t in HASHTAG.findall(text or "")]
    tab = next((TAGS[t] for t in tags if t in TAGS), None)
    if tab == "efiry" and "луксмаксинг" in tags:
        return "lx"
    return tab


def media_kind(msg):
    return next((k for k in MEDIA if k in msg), None)


# ---------------- оглавление ----------------
def find_item(lib, url):
    for tab in lib:
        for g in tab["groups"]:
            for it in g["items"]:
                if it["u"] == url:
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


def add_item(lib, code, p):
    tab_key, sub, _ = SECTIONS[code]
    remove_url(lib, p["u"])
    tab = next(t for t in lib if t["key"] == tab_key)
    group = next((g for g in tab["groups"] if g["title"] == sub), None)
    if group is None:
        group = {"title": sub, "items": []}
        tab["groups"].append(group) if sub else tab["groups"].insert(0, group)
    group["items"].insert(0, {"t": p["t"], "d": p["d"], "iso": p["iso"], "u": p["u"], "auto": True})


# ---------------- сообщения модератору ----------------
def kb_choose(mid):
    b = lambda text, data: {"text": text, "callback_data": data}
    return {"inline_keyboard": [
        [b("🎙 Эфир", f"a|efiry|{mid}"), b("🎧 Подкаст", f"a|podcasts|{mid}")],
        [b("📖 Текст", f"a|texts|{mid}"), b("🔐 Закрытые", f"a|closed|{mid}")],
        [b("🗿 Эфир · Луксмаксинг", f"a|lx|{mid}")],
        [b("⏭ Пропустить", f"s|{mid}")],
    ]}


def card(p):
    head = {
        "new": "📝 <b>Новый пост в канале</b>",
        "added": f"✅ <b>Добавлено в оглавление → {SECTIONS.get(p.get('sec'), (0, 0, '?'))[2]}</b>",
        "skipped": "⏭ <b>Пропущено</b>",
    }[p["status"]]
    kind = f" · {p['kind']}" if p.get("kind") else ""
    body = f"{head}\n\n<b>{html.escape(p['t'])}</b>\n<i>{p['d']}{kind}</i>"
    if p.get("excerpt"):
        body += f"\n\n{html.escape(p['excerpt'])}"
    body += f"\n\n{p['u']}"
    if p["status"] == "new":
        body += "\n\nКуда добавить? Чтобы задать своё название, ответь на это сообщение."
        kb = kb_choose(p["mid"])
    elif p["status"] == "added":
        body += "\n\n✏️ Своё название: ответь на это сообщение."
        kb = {"inline_keyboard": [[{"text": "↩️ Убрать из оглавления", "callback_data": f"u|{p['mid']}"}]]}
    else:
        kb = {"inline_keyboard": [[{"text": "➕ Всё-таки добавить", "callback_data": f"r|{p['mid']}"}]]}
    return body, kb


def notify(p, moderators):
    text, kb = card(p)
    p.setdefault("msgs", {})
    for uid in moderators:
        r = api("sendMessage", chat_id=uid, text=text, parse_mode="HTML",
                reply_markup=kb, link_preview_options={"is_disabled": True})
        if r:
            p["msgs"][str(uid)] = r["message_id"]


def refresh(p):
    text, kb = card(p)
    for uid, mid in p.get("msgs", {}).items():
        api("editMessageText", chat_id=int(uid), message_id=mid, text=text, parse_mode="HTML",
            reply_markup=kb, link_preview_options={"is_disabled": True})


# ---------------- главный цикл ----------------
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = load_json(CONFIG, {"moderators": []})
    mods = [int(x) for x in cfg.get("moderators", [])]
    lib = load_library()
    state = load_json(STATE, {"offset": 0})
    posts = state.setdefault("posts", {})
    log = []
    updates = get_updates(state["offset"])

    for u in updates:
        state["offset"] = max(state["offset"], u["update_id"] + 1)

        # --- новый / отредактированный пост канала ---
        msg = u.get("channel_post") or u.get("edited_channel_post")
        if msg:
            if "channel_id" in cfg and msg["chat"]["id"] != cfg["channel_id"]:
                continue
            text = msg.get("text") or msg.get("caption") or ""
            if not text and not media_kind(msg):
                continue  # служебные сообщения (закреп и т.п.)
            mid = str(msg["message_id"])
            group = msg.get("media_group_id")
            if group:  # альбом: одна карточка на весь альбом
                first = next((k for k, v in posts.items() if v.get("group") == group), None)
                if first and first != mid:
                    if text and not posts[first].get("custom"):
                        posts[first]["t"] = clean_title(text) or posts[first]["t"]
                        posts[first]["excerpt"] = text[:300]
                        refresh(posts[first])
                    continue
            dt = datetime.fromtimestamp(msg["date"], MSK)
            p = posts.get(mid)
            edited = "edited_channel_post" in u
            if p is None:
                p = posts[mid] = {
                    "mid": mid, "u": post_url(msg["chat"], msg["message_id"]), "group": group,
                    "t": clean_title(text) or f"Пост от {dt:%d.%m}", "d": dt.strftime("%d.%m.%y"),
                    "iso": dt.strftime("%Y-%m-%d"), "ts": msg["date"], "kind": media_kind(msg),
                    "excerpt": text[:300], "status": "new",
                }
            elif not p.get("custom"):
                p["t"] = clean_title(text) or p["t"]
                p["excerpt"] = text[:300]
            sec = tag_section(text)
            if sec and p["status"] != "added":
                p["status"], p["sec"] = "added", sec
                add_item(lib, sec, p)
                log.append(f"+ по хэштегу [{sec}] {p['t']}")
            elif p["status"] == "added":
                it = find_item(lib, p["u"])
                if it:
                    it["t"] = p["t"]
            if p.get("msgs"):
                refresh(p)
            elif not edited:
                notify(p, mods)
                log.append(f"? на модерацию: {p['t']}")
            continue

        # --- нажатие кнопки ---
        cq = u.get("callback_query")
        if cq:
            uid = cq["from"]["id"]
            parts = cq.get("data", "").split("|")
            p = posts.get(parts[-1])
            if uid not in mods or p is None:
                api("answerCallbackQuery", callback_query_id=cq["id"], text="Нет доступа или пост устарел")
                continue
            act = parts[0]
            if act == "a":
                p["status"], p["sec"] = "added", parts[1]
                add_item(lib, parts[1], p)
                note = "Добавлено"
                log.append(f"+ [{parts[1]}] {p['t']}")
            elif act == "u":
                remove_url(lib, p["u"])
                p["status"] = "new"
                note = "Убрано"
                log.append(f"- убрано {p['t']}")
            elif act == "s":
                p["status"] = "skipped"
                note = "Пропущено"
            else:  # r
                p["status"] = "new"
                note = "Выбери раздел"
            api("answerCallbackQuery", callback_query_id=cq["id"], text=note)
            refresh(p)
            continue

        # --- личные сообщения модератора ---
        m = u.get("message")
        if m and m.get("chat", {}).get("type") == "private":
            uid = m["from"]["id"]
            if uid not in mods:
                api("sendMessage", chat_id=uid, text="Это служебный бот оглавления. Открыть оглавление: https://t.me/zamok_zakrep_bot/menu")
                continue
            rep = m.get("reply_to_message")
            new_title = (m.get("text") or "").strip()
            if rep and new_title:
                p = next((v for v in posts.values() if v.get("msgs", {}).get(str(uid)) == rep["message_id"]), None)
                if p:
                    p["t"], p["custom"] = new_title[:90], True
                    it = find_item(lib, p["u"])
                    if it:
                        it["t"] = p["t"]
                    refresh(p)
                    api("sendMessage", chat_id=uid, text=f"✏️ Название: {p['t']}", reply_to_message_id=m["message_id"])
                    log.append(f"~ название: {p['t']}")
                    continue
            if new_title.startswith("/start"):
                api("sendMessage", chat_id=uid, text="👋 Готово. Новые посты канала буду присылать сюда с кнопками разделов: ты решаешь, что попадёт в оглавление.")

    # чистим старые карточки
    now = time.time()
    for k in [k for k, v in posts.items() if now - v.get("ts", now) > PENDING_TTL]:
        del posts[k]

    save_library(lib)
    json.dump(state, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"updates: {len(updates)}, offset: {state['offset']}")
    print("\n".join(log) or "оглавление без изменений")


if __name__ == "__main__":
    main()
