#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# github_radar.py — радар трендов GitHub с памятью наблюдений и надзором Claude.

import os
import json
import sqlite3
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

# ============== НАСТРОЙКИ ==============
# Секреты — в .env (файл в .gitignore). См. .env.example
def _load_local_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

_load_local_env()
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
TOPICS = ["ai-agents", "llm", "rag", "mcp", "saas", "automation"]
DAYS = 7
MIN_STARS = 30
SEND_DIGEST = True
PUBLISH_DASHBOARD = True       # авто-публикация index.html на GitHub Pages
SPIKE_THRESHOLD = 20
ANALYZE_TOP = 5
MEMORY_DAYS = 7              # сколько дней Claude помнит свои наблюдения
CLAUDE_MODEL = "claude-sonnet-4-5-20250929"
# ======================================

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.db")
API = "https://api.github.com/search/repositories"


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS snapshots (
        repo TEXT, stars INTEGER, language TEXT, description TEXT,
        url TEXT, created_at TEXT, seen_at TEXT,
        PRIMARY KEY (repo, seen_at))""")
    con.execute("""CREATE TABLE IF NOT EXISTS observations (
        seen_at TEXT, repo TEXT, note TEXT,
        PRIMARY KEY (seen_at, repo))""")
    con.commit()
    return con


def save_snapshot(con, repos, seen_at):
    for r in repos:
        con.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?)",
            (r["full_name"], r["stargazers_count"], r.get("language"),
             (r.get("description") or "")[:300], r["html_url"],
             r["created_at"], seen_at))
    con.commit()


def previous_stars(con, repo, before):
    row = con.execute("SELECT stars FROM snapshots WHERE repo=? AND seen_at<? "
        "ORDER BY seen_at DESC LIMIT 1", (repo, before)).fetchone()
    return row[0] if row else None


def past_observations(con):
    """Наблюдения Claude за последние MEMORY_DAYS дней — его память."""
    rows = con.execute(
        "SELECT seen_at, repo, note FROM observations "
        "WHERE seen_at >= date('now', ?) ORDER BY seen_at",
        (f"-{MEMORY_DAYS} days",)).fetchall()
    return rows


def save_observations(con, seen_at, obs):
    """obs: список (repo, note)."""
    for repo, note in obs:
        con.execute("INSERT OR REPLACE INTO observations VALUES (?,?,?)",
                    (seen_at, repo, note))
    con.commit()


def gh_request(query):
    params = urllib.parse.urlencode(
        {"q": query, "sort": "stars", "order": "desc", "per_page": 30})
    req = urllib.request.Request(f"{API}?{params}")
    req.add_header("Accept", "application/vnd.github+json")
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp).get("items", [])
    except Exception as e:
        print(f"[!] GitHub API: {e}")
        return []


def collect():
    since = (datetime.now(timezone.utc) - timedelta(days=DAYS)).strftime("%Y-%m-%d")
    repos = {}
    for t in TOPICS:
        q = f"topic:{t} created:>{since} stars:>{MIN_STARS}"
        for r in gh_request(q):
            repos[r["full_name"]] = r
    return list(repos.values())


def _client():
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        print("[!] Нет ANTHROPIC_API_KEY.")
        return None
    try:
        import anthropic
        return anthropic.Anthropic(api_key=key)
    except ImportError:
        print("[!] Не установлен anthropic.")
        return None


def analyze_with_claude(client, items):
    if not client or not items:
        return {}
    lines = [f"- {r['full_name']} ({r.get('language') or '-'}): "
             f"{(r.get('description') or '')[:150]}" for _, r in items]
    prompt = (
        "Ты помогаешь разработчику-предпринимателю следить за трендами GitHub. "
        "По КАЖДОМУ репозиторию дай ОДНУ короткую строку: что это простыми словами "
        "и чем полезно/интересно (идея, приём, инструмент). До 20 слов.\n\n"
        "КРИТИЧЕСКИ ВАЖНО: пиши ТОЛЬКО на русском, даже если описание английское. "
        "Названия репозиториев оставляй как есть.\n"
        "Формат строго: full_name — разбор на русском. Одна строка на репозиторий.\n\n"
        + "\n".join(lines))
    try:
        msg = client.messages.create(model=CLAUDE_MODEL, max_tokens=1500,
            messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in msg.content if b.type == "text")
    except Exception as e:
        print(f"[!] Ошибка анализа Claude: {e}")
        return {}
    names = [r["full_name"] for _, r in items]
    result = {}
    for line in text.splitlines():
        line = line.strip().lstrip("-•* ").strip()
        if not line:
            continue
        for name in names:
            if line.startswith(name):
                rest = line[len(name):].lstrip(" :—–-·").strip()
                if rest:
                    result[name] = rest
                break
    return result


def supervise_with_claude(client, repos, movers, past_obs):
    """Надзор: Claude ведёт тренды во времени, помнит прошлые наблюдения,
    выдаёт ежедневный совет + новые наблюдения для записи в память.
    Возвращает (текст_совета, список_новых_наблюдений)."""
    if not client:
        return "", []

    # текущая картина
    cur_lines = []
    src = movers if movers else [(r["stargazers_count"], r) for r in
        sorted(repos, key=lambda x: x["stargazers_count"], reverse=True)]
    for val, r in src[:20]:
        cur_lines.append(f"- {r['full_name']} ({r.get('language') or '-'}), "
                         f"+{val}: {(r.get('description') or '')[:100]}")
    current = "\n".join(cur_lines)

    # память прошлых наблюдений
    if past_obs:
        mem = "\n".join(f"- [{d}] {repo}: {note}" for d, repo, note in past_obs)
    else:
        mem = "(пока нет прошлых наблюдений — это первые дни)"

    prompt = (
        "Ты — личный аналитик-наблюдатель за трендами GitHub для разработчика-"
        "предпринимателя, который ищет нишу для своего SaaS. Ты ведёшь наблюдение "
        "ВО ВРЕМЕНИ: помнишь, на что указывал раньше, и проверяешь, подтверждается ли.\n\n"
        "ТВОИ ПРОШЛЫЕ НАБЛЮДЕНИЯ (за неделю):\n" + mem + "\n\n"
        "СЕГОДНЯШНЯЯ КАРТИНА (растущие проекты):\n" + current + "\n\n"
        "Сделай две вещи:\n\n"
        "1) НАПИШИ СОВЕТ на русском (4-6 предложений, без списков), заголовок не нужен:\n"
        "   - что из прошлых наблюдений ПОДТВЕРДИЛОСЬ или усилилось (растёт несколько дней);\n"
        "   - что НОВОЕ появилось сегодня и заслуживает внимания;\n"
        "   - КОНКРЕТНО на что обратить внимание именно сегодня и ПОЧЕМУ "
        "(идея, ниша, приём). Будь конкретным, называй проекты/темы.\n\n"
        "2) В КОНЦЕ отдельным блоком после строки '###OBS###' перечисли наблюдения "
        "для памяти (то, за чем стоит следить дальше), строго в формате:\n"
        "full_name | короткая заметка\n"
        "Одно наблюдение на строку, 2-5 штук, только реально важное. "
        "Заметки на русском."
    )
    try:
        msg = client.messages.create(model=CLAUDE_MODEL, max_tokens=900,
            messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
    except Exception as e:
        print(f"[!] Ошибка надзора Claude: {e}")
        return "", []

    # разделяем совет и наблюдения
    advice, obs = text, []
    if "###OBS###" in text:
        advice, _, obs_block = text.partition("###OBS###")
        advice = advice.strip()
        for line in obs_block.splitlines():
            line = line.strip().lstrip("-•* ").strip()
            if "|" in line:
                repo, _, note = line.partition("|")
                repo, note = repo.strip(), note.strip()
                if repo and note:
                    obs.append((repo, note))
    return advice, obs


def send_telegram(text):
    if not TG_TOKEN or TG_TOKEN == "ВСТАВЬ_ТОКЕН_БОТА":
        print("[!] Не задан TG_TOKEN (.env или переменная окружения). Вот что было бы отправлено:\n")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as resp:
            res = json.load(resp)
            print("[ok] Отправлено в Telegram." if res.get("ok") else f"[!] Telegram: {res}")
    except Exception as e:
        print(f"[!] Ошибка отправки: {e}")


def fmt(r, prefix, analysis):
    desc = (r.get("description") or "").replace("<","").replace(">","")[:80]
    body = analysis if analysis else desc
    return (f"<b>{prefix}</b> <a href=\"{r['html_url']}\">{r['full_name']}</a> "
            f"({r.get('language') or '-'})\n{body}")


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    con = init_db()

    repos = collect()
    if not repos:
        print("Ничего не собрано.")
        return

    movers = []
    for r in repos:
        prev = previous_stars(con, r["full_name"], today)
        if prev is not None:
            delta = r["stargazers_count"] - prev
            if delta > 0:
                movers.append((delta, r))

    save_snapshot(con, repos, today)
    movers.sort(key=lambda x: x[0], reverse=True)
    spikes = [(d, r) for d, r in movers if d >= SPIKE_THRESHOLD]

    client = _client()

    # разбор топ-проектов
    if movers:
        top_for_analysis = movers[:ANALYZE_TOP]
    else:
        top_for_analysis = [(r["stargazers_count"], r) for r in
            sorted(repos, key=lambda x: x["stargazers_count"], reverse=True)[:ANALYZE_TOP]]
    to_analyze = {id(r): (d, r) for d, r in spikes}
    for d, r in top_for_analysis:
        to_analyze[id(r)] = (d, r)
    analyzed = analyze_with_claude(client, list(to_analyze.values()))

    # НАДЗОР: ведём тренды во времени
    past_obs = past_observations(con)
    advice, new_obs = supervise_with_claude(client, repos, movers, past_obs)
    if new_obs:
        save_observations(con, today, new_obs)

    now_str = datetime.now().strftime("%d.%m %H:%M")
    lines = []

    if spikes:
        lines.append(f"\U0001F680 <b>ВСПЛЕСК на GitHub</b> ({now_str})")
        for delta, r in spikes[:10]:
            lines.append(fmt(r, f"+{delta}\u2B50", analyzed.get(r["full_name"])))

    if SEND_DIGEST:
        lines.append(f"\U0001F4E1 <b>Радар GitHub</b> ({now_str}) — топ растущих:"
                     if not spikes else "\u2014 \u2014 \u2014\n\U0001F4E1 Остальной топ:")
        if movers:
            top = movers; star = True
        else:
            top = [(r["stargazers_count"], r) for r in
                   sorted(repos, key=lambda x: x["stargazers_count"], reverse=True)]
            star = False
        for val, r in top[:10]:
            prefix = f"+{val}\u2B50" if star else f"{val}\u2B50"
            lines.append(fmt(r, prefix, analyzed.get(r["full_name"])))

    # блок надзора — главное
    if advice:
        lines.append(f"\U0001F3AF <b>Сегодня обрати внимание:</b>\n{advice}")

    if not lines:
        print("Нечего отправлять.")
        return

    message = "\n\n".join(lines)
    if len(message) > 4000:
        message = message[:3950] + "\n\n\u2026(обрезано)"
    send_telegram(message)

    total = con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    days_tracked = con.execute("SELECT COUNT(DISTINCT seen_at) FROM snapshots").fetchone()[0]
    obs_count = con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    print(f"[база] записей: {total} | дней истории: {days_tracked} | наблюдений: {obs_count}")

    if PUBLISH_DASHBOARD:
        from dashboard import publish_to_github
        publish_to_github()


if __name__ == "__main__":
    main()