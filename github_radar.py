#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# github_radar.py — радар трендов GitHub с памятью наблюдений и надзором Claude.

import os
import re
import json
import sqlite3
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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
TZ = ZoneInfo("Asia/Almaty")
MIN_MRR = 500                  # TrustMRR: минимальный MRR для отбора (фолбэк 100)
# ======================================

TRUSTMRR_URL = "https://trustmrr.com/"
TRUSTMRR_CATEGORIES = {
    "AI": ["ai", "ml", "gpt", "llm", "bot", "automation", "learning", "neural",
           "artificial intelligence", "machine learning"],
    "SaaS": ["saas", "software", "cloud", "subscription", "b2b", "b2c", "platform"],
    "Developer Tools": ["developer", "devops", "api", "sdk", "ide", "git", "engineering",
                        "code", "programming", "terminal"],
}
TRUSTMRR_TARGET_CATEGORIES = ["AI", "SaaS", "Developer Tools"]
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
    con.execute("""CREATE TABLE IF NOT EXISTS advices (
        seen_at TEXT PRIMARY KEY, advice TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS hn (
        seen_at TEXT, hn_id TEXT, title TEXT, points INTEGER,
        comments INTEGER, url TEXT, PRIMARY KEY (seen_at, hn_id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS trustmrr (
        seen_at TEXT, name TEXT, mrr REAL, growth REAL, category TEXT,
        url TEXT, for_sale INTEGER, PRIMARY KEY (seen_at, name))""")
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


def collect_hn():
    """Истории Hacker News по темам через официальный Algolia API."""
    queries = ["AI agents", "LLM", "RAG", "MCP", "SaaS", "automation"]
    seen = {}
    for q in queries:
        url = ("https://hn.algolia.com/api/v1/search?"
               + urllib.parse.urlencode({
                   "query": q, "tags": "story", "hitsPerPage": 20}))
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = json.load(resp)
        except Exception as e:
            print(f"[!] HN API ({q}): {e}")
            continue
        added = 0
        for h in data.get("hits", []):
            if (h.get("points") or 0) <= 50:
                continue
            oid = h.get("objectID")
            if not oid or oid in seen:
                continue
            seen[oid] = {
                "title": h.get("title") or "",
                "points": h.get("points") or 0,
                "comments": h.get("num_comments") or 0,
                "url": h.get("url") or f"https://news.ycombinator.com/item?id={oid}",
                "hn_url": f"https://news.ycombinator.com/item?id={oid}",
            }
            added += 1
            if added >= 5:
                break
    items = sorted(seen.values(), key=lambda x: x["points"], reverse=True)
    return items[:10]


def _trustmrr_field(chunk, key):
    m = re.search(
        rf'"{re.escape(key)}":(null|true|false|[-\d.eE+]+|"((?:\\.|[^"\\])*)")', chunk)
    if not m:
        return None
    if m.group(1) == "null":
        return None
    if m.group(1) in ("true", "false"):
        return m.group(1) == "true"
    if m.group(2) is not None:
        try:
            return json.loads(f'"{m.group(2)}"')
        except json.JSONDecodeError:
            return m.group(2).replace("\\n", " ").replace('\\"', '"')
    return float(m.group(1))


def _trustmrr_category(name, description):
    text = f"{name} {description or ''}".lower()
    for category in TRUSTMRR_TARGET_CATEGORIES:
        if any(kw in text for kw in TRUSTMRR_CATEGORIES[category]):
            return category
    return None


def _trustmrr_parse_rsc(html):
    startups = {}
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S)
    for raw in chunks:
        dec = bytes(raw, "utf-8").decode("unicode_escape", errors="replace")
        pos = 0
        while True:
            idx = dec.find('"_id":"', pos)
            if idx < 0:
                break
            chunk = dec[idx:idx + 3500]
            if '"slug":"' not in chunk or '"currentMrr"' not in chunk:
                pos = idx + 6
                continue
            slug = _trustmrr_field(chunk, "slug")
            name = _trustmrr_field(chunk, "name")
            if not slug or not name:
                pos = idx + 6
                continue
            growth = _trustmrr_field(chunk, "cachedGrowthMRR30d")
            if growth is None:
                growth = _trustmrr_field(chunk, "growthMRR30d")
            startups[slug] = {
                "name": name,
                "slug": slug,
                "mrr": float(_trustmrr_field(chunk, "currentMrr") or 0),
                "growth": float(growth or 0),
                "description": _trustmrr_field(chunk, "description") or "",
                "on_sale": bool(_trustmrr_field(chunk, "onSale")),
            }
            pos = idx + 6
    return list(startups.values())


def collect_trustmrr():
    """Top TrustMRR startups by MoM MRR growth from homepage RSC payload."""
    req = urllib.request.Request(
        TRUSTMRR_URL,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[!] TrustMRR: {e}")
        return []

    items = []
    for s in _trustmrr_parse_rsc(html):
        category = _trustmrr_category(s["name"], s["description"])
        if category not in TRUSTMRR_TARGET_CATEGORIES:
            continue
        items.append({
            "name": s["name"],
            "mrr": round(s["mrr"], 2),
            "growth": round(s["growth"], 2),
            "category": category,
            "url": f"https://trustmrr.com/startup/{s['slug']}",
            "for_sale": s["on_sale"],
        })

    def _top(min_mrr):
        filtered = [x for x in items if x["mrr"] >= min_mrr]
        filtered.sort(key=lambda x: (x["growth"], x["mrr"]), reverse=True)
        return filtered[:10]

    result = _top(MIN_MRR)
    if len(result) < 5:
        result = _top(100)
    return result


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


def supervise_with_claude(client, repos, movers, past_obs, hn_items=None, trustmrr_items=None):
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

    if hn_items:
        hn_lines = [f"- [{h['points']} очков, {h['comments']} комм.] {h['title']}"
                    for h in hn_items[:8]]
        hn_block = "\n".join(hn_lines)
    else:
        hn_block = "(нет данных HN)"

    if trustmrr_items:
        tm_lines = []
        for t in trustmrr_items[:8]:
            sale = ", продаётся" if t.get("for_sale") else ""
            tm_lines.append(
                f"- {t['name']} ({t['category']}): MRR ${t['mrr']:,.0f}, "
                f"рост MoM {t['growth']:+.1f}%{sale}")
        tm_block = "\n".join(tm_lines)
    else:
        tm_block = "(нет данных TrustMRR)"

    # память прошлых наблюдений
    if past_obs:
        mem = "\n".join(f"- [{d}] {repo}: {note}" for d, repo, note in past_obs)
    else:
        mem = "(пока нет прошлых наблюдений — это первые дни)"

    real_days = len(set(d for d, _, _ in past_obs)) if past_obs else 0

    prompt = (
        "Ты — личный аналитик-наблюдатель за трендами GitHub для разработчика-"
        "предпринимателя, который ищет нишу для своего SaaS. Ты ведёшь наблюдение "
        "ВО ВРЕМЕНИ: помнишь, на что указывал раньше, и проверяешь, подтверждается ли.\n\n"
        f"КРИТИЧЕСКИ ВАЖНО: реальных календарных дней с данными всего {real_days}. "
        "ЗАПРЕЩЕНО писать 'седьмой день', 'восьмой день' и любое число дней больше "
        f"{real_days}. Считай дни ТОЛЬКО по разным датам в наблюдениях, а не по числу "
        "записей. Если дней 1-2 — пиши 'наблюдение только началось, выводы предварительные'. "
        "Лучше честно сказать 'данных мало', чем преувеличить.\n\n"
        "ТВОИ ПРОШЛЫЕ НАБЛЮДЕНИЯ (за неделю):\n" + mem + "\n\n"
        "СЕГОДНЯШНЯЯ КАРТИНА GITHUB (что строят):\n" + current + "\n\n"
        "ЧТО ОБСУЖДАЮТ НА HACKER NEWS (настроения, тревоги, запуски):\n" + hn_block + "\n\n"
        "ЧТО УЖЕ ЗАРАБАТЫВАЕТ НА TRUSTMRR (только MRR $500+, проверенная выручка):\n"
        + tm_block + "\n\n"
        "ЗАДАЧА: не обзор рынка, а ОДНА конкретная возможность с проверяемым действием.\n\n"
        "ПРАВИЛА:\n"
        "- Выбери ОДНУ самую сильную нишу на стыке трёх источников — не перечисляй 5 трендов.\n"
        "- СИГНАЛ = что СТРОЯТ (GitHub) + что ОБСУЖДАЮТ/чего боятся (HN, с очками) + "
        "что ЗАРАБАТЫВАЕТ похожее (TrustMRR с $MRR).\n"
        "- КТО ПЛАТИТ: назови реальный проект из TrustMRR и его MRR как доказательство денег в нише. "
        "Если в TrustMRR нет аналога с MRR $500+ — напиши «деньги в нише пока не подтверждены данными».\n"
        "- ПЕРВЫЙ ШАГ: одно проверяемое действие на эту неделю без полной разработки "
        "(лендинг, N интервью, пост в конкретное сообщество — конкретно).\n"
        "- ЗАПРЕЩЕНО: «выглядит перспективно», «стоит обратить внимание», «может быть нишей» "
        "без цифр и фактов из данных выше.\n"
        f"- Если дней данных {real_days} ≤ 2 — явно напиши, что выводы предварительные "
        "и тренд во времени не подтверждён. Если данных мало для уверенного вывода — "
        "честно скажи «данных пока мало», не имитируй уверенность.\n\n"
        "ФОРМАТ СОВЕТА (строго, на русском):\n"
        "НИША: [одна конкретная ниша]\n"
        "СИГНАЛ: строят [проект GitHub], обсуждают [тема HN, очки], "
        "зарабатывает [проект TrustMRR, $MRR]\n"
        "ПОЧЕМУ СЕЙЧАС: [1-2 предложения с фактами]\n"
        "ПЕРВЫЙ ШАГ: [одно действие на эту неделю]\n\n"
        "После совета добавь блок наблюдений для памяти — отдельно после строки '###OBS###':\n"
        "full_name | короткая заметка\n"
        "2-5 строк, только важное, на русском."
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


def tg_html(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def split_message(text, limit=4000):
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for block in text.split("\n\n"):
        chunk = block if not current else f"{current}\n\n{block}"
        if len(chunk) <= limit:
            current = chunk
            continue
        if current:
            parts.append(current)
        current = block if len(block) <= limit else block[:limit]
    if current:
        parts.append(current)
    return parts


def send_telegram(text):
    if not TG_TOKEN or TG_TOKEN == "ВСТАВЬ_ТОКЕН_БОТА":
        print("[!] Не задан TG_TOKEN (.env или переменная окружения). Вот что было бы отправлено:\n")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for part in split_message(text):
        data = urllib.parse.urlencode({
            "chat_id": TG_CHAT_ID, "text": part,
            "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as resp:
                res = json.load(resp)
                if not res.get("ok"):
                    print(f"[!] Telegram: {res}")
                    return
        except Exception as e:
            err = e.read().decode() if hasattr(e, "read") else str(e)
            print(f"[!] Ошибка отправки: {e}")
            if err:
                print(err[:500])
            return
    print("[ok] Отправлено в Telegram.")


def fmt(r, prefix, analysis):
    desc = tg_html((r.get("description") or "")[:80])
    body = tg_html(analysis) if analysis else desc
    return (f"<b>{prefix}</b> <a href=\"{r['html_url']}\">{r['full_name']}</a> "
            f"({r.get('language') or '-'})\n{body}")


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    con = init_db()
    repos = []
    try:
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
        hn_items = collect_hn()
        trustmrr_items = collect_trustmrr()
        past_obs = past_observations(con)
        advice, new_obs = supervise_with_claude(
            client, repos, movers, past_obs, hn_items, trustmrr_items)
        if new_obs:
            save_observations(con, today, new_obs)
        if advice:
            con.execute("INSERT OR REPLACE INTO advices VALUES (?,?)", (today, advice))
            con.commit()

        now_str = datetime.now(TZ).strftime("%d.%m %H:%M")
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

        # Hacker News — отдельный блок
        if hn_items:
            for h in hn_items:
                con.execute("INSERT OR REPLACE INTO hn VALUES (?,?,?,?,?,?)",
                    (today, h["hn_url"].split("=")[-1], h["title"],
                     h["points"], h["comments"], h["hn_url"]))
            con.commit()
            lines.append("\u2014 \u2014 \u2014\n\U0001F4F0 <b>Hacker News</b> — что обсуждают:")
            for h in hn_items[:8]:
                title = tg_html(h["title"][:90])
                lines.append(f"<b>{h['points']}\u25B2</b> <a href=\"{h['hn_url']}\">{title}</a> "
                             f"({h['comments']} коммент.)")

        # TrustMRR — отдельный блок
        if trustmrr_items:
            for t in trustmrr_items:
                con.execute("INSERT OR REPLACE INTO trustmrr VALUES (?,?,?,?,?,?,?)",
                    (today, t["name"], t["mrr"], t["growth"], t["category"],
                     t["url"], 1 if t["for_sale"] else 0))
            con.commit()
            lines.append("\u2014 \u2014 \u2014\n\U0001F4B0 <b>TrustMRR</b> \u2014 что зарабатывает:")
            for t in trustmrr_items[:8]:
                sale = " \U0001F3F7\uFE0F" if t["for_sale"] else ""
                name = tg_html(t["name"][:50])
                growth_sign = f"{t['growth']:+.1f}%"
                lines.append(
                    f"<b>{growth_sign}</b> <a href=\"{t['url']}\">{name}</a> "
                    f"${t['mrr']:,.0f} MRR \u00b7 {tg_html(t['category'])}{sale}")

        # блок надзора — главное
        if advice:
            lines.append(f"\U0001F3AF <b>Сегодня обрати внимание:</b>\n{tg_html(advice)}")

        if not lines:
            print("Нечего отправлять.")
        else:
            send_telegram("\n\n".join(lines))

        total = con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        days_tracked = con.execute("SELECT COUNT(DISTINCT seen_at) FROM snapshots").fetchone()[0]
        obs_count = con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        print(f"[база] записей: {total} | дней истории: {days_tracked} | наблюдений: {obs_count}")
    finally:
        con.close()

    if PUBLISH_DASHBOARD and repos:
        from dashboard import publish_to_github
        publish_to_github()


if __name__ == "__main__":
    main()