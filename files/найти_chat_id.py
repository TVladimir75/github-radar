#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
найти_chat_id.py — определяет твой Telegram chat_id.

КАК ПОЛЬЗОВАТЬСЯ:
  1. Создай бота в @BotFather, получи токен.
  2. Напиши своему боту в Telegram что-нибудь (например "привет"). ВАЖНО.
  3. Вставь токен ниже в кавычки (строка TOKEN).
  4. Запусти:  python3 найти_chat_id.py
  5. Скрипт покажет твой chat_id — запиши его, он нужен для основного скрипта.
"""

import json
import urllib.request

# ВСТАВЬ СВОЙ ТОКЕН СЮДА (между кавычек):
TOKEN = "ВСТАВЬ_ТОКЕН_СЮДА"


def main():
    if TOKEN == "ВСТАВЬ_ТОКЕН_СЮДА":
        print("Сначала вставь токен бота в переменную TOKEN в этом файле.")
        return

    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.load(resp)
    except Exception as e:
        print(f"Ошибка обращения к Telegram: {e}")
        print("Проверь, что токен правильный.")
        return

    if not data.get("ok"):
        print("Telegram вернул ошибку. Проверь токен.")
        print(data)
        return

    updates = data.get("result", [])
    if not updates:
        print("Сообщений не найдено.")
        print("Напиши своему боту 'привет' в Telegram и запусти скрипт снова.")
        return

    # Собираем все уникальные chat_id из сообщений
    found = {}
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat", {})
        if "id" in chat:
            name = chat.get("first_name", "") + " " + chat.get("username", "")
            found[chat["id"]] = name.strip()

    if not found:
        print("Сообщения есть, но chat_id не найден. Напиши боту обычный текст и повтори.")
        return

    print("Найдено:")
    for cid, name in found.items():
        print(f"  chat_id = {cid}   ({name})")
    print("\nЭто число и есть твой chat_id. Впиши его в основной скрипт.")


if __name__ == "__main__":
    main()
