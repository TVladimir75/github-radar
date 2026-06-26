#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
github_radar.py — автономный радар трендов GitHub с отправкой в Telegram.

ЧТО ДЕЛАЕТ:
  Сам собирает свежие/быстрорастущие репозитории GitHub по твоим темам,
  копит их историю в локальную базу (history.db), при каждом запуске
  сравнивает с прошлым снимком, ловит резкий рост звёзд и присылает
  результат тебе в Telegram. Запускается по расписанию — терминал не нужен.

НАСТРОЙКА (один раз — впиши свои значения ниже):
  TG_TOKEN    — токен бота из @BotFather (актуальный!)
  TG_CHAT_ID  — твой chat_id (у тебя: 543789742)
  TOPICS      — темы радара (уже заполнены)

ЗАПУСК ВРУЧНУЮ:  python3 github_radar.py
АВТОЗАПУСК: см. setup_schedule.txt
Зависимости: только стандартная библиотека Python.
"""

import os
import json
import sqlite3
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

# ============== НАСТРОЙКИ — ВПИШИ СВОИ ЗНАЧЕНИЯ ==============
TG_TOKEN   = "ВСТАВЬ_ТОКЕН_БОТА"      # из @BotFather
TG_CHAT_ID = "543789742"              # твой chat_id
TOPICS = ["ai-agents", "llm", "rag", "mcp", "saas", "automation"]
DAYS = 7
MIN_STARS = 30
SEND_DIGEST = True       # True = слать топ всегда; False = только всплески
SPIKE_THRESHOLD = 20     # прирост звёзд, считающийся всплеском
# ============================================================

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.db")
API = "https://api.github.com/search/repositories"


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS snapshots (
        repo TEXT, stars INTEGER, language TEXT, description TEXT,
        url TEXT, created_at TEXT, seen_at TEXT,
        PRIMARY KEY (repo, seen_at))""")
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


def send_telegram(text):
    if TG_TOKEN == "ВСТАВЬ_ТОКЕН_БОТА":
        print("[!] Не вписан токен бота (TG_TOKEN). Вот что было бы отправлено:\n")
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


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    con = init_db()

    repos = collect()
    if not repos:
        print("Ничего не собрано (лимит API или нет результатов).")
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

    now_str = datetime.now().strftime("%d.%m %H:%M")
    lines = []

    if spikes:
        lines.append(f"\U0001F680 <b>ВСПЛЕСК на GitHub</b> ({now_str})")
        for delta, r in spikes[:10]:
            desc = (r.get("description") or "").replace("<","").replace(">","")[:90]
            lines.append(f"<b>+{delta}\u2B50</b> <a href=\"{r['html_url']}\">{r['full_name']}</a> "
                         f"({r.get('language') or '-'})\n{desc}")

    if SEND_DIGEST:
        lines.append(f"\U0001F4E1 <b>Радар GitHub</b> ({now_str}) — топ растущих:"
                     if not spikes else "\u2014 \u2014 \u2014\n\U0001F4E1 Остальной топ:")
        if movers:
            top = movers
            star_prefix = True
        else:
            top = [(r["stargazers_count"], r) for r in
                   sorted(repos, key=lambda x: x["stargazers_count"], reverse=True)]
            star_prefix = False
        for val, r in top[:10]:
            desc = (r.get("description") or "").replace("<","").replace(">","")[:80]
            prefix = f"+{val}\u2B50" if star_prefix else f"{val}\u2B50"
            lines.append(f"<b>{prefix}</b> <a href=\"{r['html_url']}\">{r['full_name']}</a> "
                         f"({r.get('language') or '-'})\n{desc}")

    if not lines:
        print("Всплесков нет, дайджест выключен — ничего не отправлено.")
        return

    message = "\n\n".join(lines)
    if len(message) > 4000:
        message = message[:3950] + "\n\n\u2026(обрезано)"

    send_telegram(message)

    total = con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    days_tracked = con.execute("SELECT COUNT(DISTINCT seen_at) FROM snapshots").fetchone()[0]
    print(f"[база] записей: {total} | дней истории: {days_tracked}")


if __name__ == "__main__":
    main()
