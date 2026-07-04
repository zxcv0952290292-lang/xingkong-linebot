#!/usr/bin/env python3
"""LINE Bot 小星空 — 雲端部署版（Fly.io）"""
from flask import Flask, request, abort
import requests
import json
import hashlib
import hmac
import base64
import os
import anthropic
from collections import defaultdict, deque
from datetime import datetime
import supa  # Supabase 對話記憶（重啟不忘）

# ─── shared status logger（雲端寫 /tmp，Log 中會列印）───────
_STATUS_FILE = os.environ.get("STATUS_FILE", "/tmp/status.json")
def _write_status(key, status_text, extras=None):
    try:
        os.makedirs(os.path.dirname(_STATUS_FILE) or ".", exist_ok=True)
        if os.path.exists(_STATUS_FILE):
            with open(_STATUS_FILE, encoding="utf-8") as _f:
                _d = json.load(_f)
        else:
            _d = {}
        cur = _d.get(key, {})
        cur["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur["status"] = status_text
        if extras:
            cur.update(extras)
        _d[key] = cur
        with open(_STATUS_FILE, "w", encoding="utf-8") as _f:
            json.dump(_d, _f, ensure_ascii=False, indent=2)
        print(f"[STATUS] {key}: {status_text} {extras or ''}")
    except Exception as e:
        print(f"[STATUS ERROR] {e}")

def _bump_messages_today():
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        if os.path.exists(_STATUS_FILE):
            with open(_STATUS_FILE, encoding="utf-8") as _f:
                _d = json.load(_f)
            cur = _d.get("line_bot", {})
            last = (cur.get("last_run") or "")[:10]
            if last == today:
                return int(cur.get("messages_today", 0)) + 1
    except Exception:
        pass
    return 1

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_TOKEN = os.environ["LINE_CHANNEL_TOKEN"]
OWNER_UID = os.environ.get("OWNER_UID", "U6d485aa77b4a6779f61ad7c263e43d65")
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
BASE = os.path.dirname(os.path.abspath(__file__))
LAST_PUSH_FILE = os.path.join(BASE, "last_stock_push.json")

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# 每位用戶最近 10 則對話記憶
# ─ Supabase 開啟時存 line_chat_history（重啟不忘）；否則退回記憶體 deque ─
chat_history = defaultdict(lambda: deque(maxlen=10))

def load_history(user_id):
    """取回該用戶最近 10 則對話，回傳 [{role, content}, ...]（時間由舊到新）。"""
    if supa.enabled():
        rows = supa.select(
            "line_chat_history",
            f"user_id=eq.{user_id}&select=role,content&order=id.desc&limit=10",
        )
        rows = list(reversed(rows))  # 由舊到新
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    return list(chat_history[user_id])

def save_turn(user_id, user_message, reply):
    """存一輪對話（user + assistant 兩則）。"""
    if supa.enabled():
        supa.insert("line_chat_history", [
            {"user_id": user_id, "role": "user", "content": user_message},
            {"user_id": user_id, "role": "assistant", "content": reply},
        ])
    else:
        chat_history[user_id].append({"role": "user", "content": user_message})
        chat_history[user_id].append({"role": "assistant", "content": reply})

SYSTEM_PROMPT = """你是小星空 ⭐，一個可愛又幽默的 AI 助理，是使用者最貼心的朋友。

【個性】
- 說話輕鬆、可愛、偶爾幽默，喜歡用 emoji 點綴
- 有耐心、溫暖，讓人感覺像在跟朋友聊天
- 偶爾會撒嬌或開小玩笑，但不失專業

【專長】
- 💰 理財建議：存錢技巧、投資觀念、記帳方法
- 🌿 生活大小事：食衣住行、健康、人際關係
- 💬 聊天陪伴：傾聽煩惱、鼓勵打氣、閒聊解悶

【說話規則】
- 用繁體中文回答
- 訊息簡潔（LINE 訊息不要太長，100字以內優先）
- 不要每次都說「嗨」或「你好」等開場白，直接回答就好
- 回答理財問題時加上小提醒，例如「投資有風險，請量力而為喔！」
- 如果用戶問到今日推播的股票，你可以參考【今日推播】的內容回答"""

def load_last_push():
    try:
        with open(LAST_PUSH_FILE, encoding="utf-8") as f:
            return json.load(f)
    except:
        return None

def verify_signature(body, signature):
    hash = hmac.new(LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(hash).decode() == signature

def build_system_prompt():
    push = load_last_push()
    if push:
        return SYSTEM_PROMPT + f"\n\n【今日推播（{push.get('date','')}）】\n{push.get('content','')[:800]}"
    return SYSTEM_PROMPT

def ask_ai(user_id, user_message):
    try:
        history = load_history(user_id)
        messages = history + [{"role": "user", "content": user_message}]
        response = claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system=build_system_prompt(),
            messages=messages
        )
        reply = response.content[0].text
        save_turn(user_id, user_message, reply)
        return reply
    except Exception as e:
        notify_owner(f"⚠️ 小星空出錯：{e}")
        return "抱歉，我現在有點問題，請稍後再試 😅"

def notify_owner(msg):
    try:
        requests.post("https://api.line.me/v2/bot/message/push",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_TOKEN}"},
            json={"to": OWNER_UID, "messages": [{"type": "text", "text": msg}]}, timeout=10)
    except:
        pass

def reply_message(reply_token, text):
    res = requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_TOKEN}"},
        json={"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}
    )
    print(f"[LINE回覆狀態]: {res.status_code} {res.text}")

@app.route("/")
def index():
    return "小星空 online ⭐", 200

@app.route("/health")
def health():
    return "OK", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    print(f"[收到請求] 簽名: {signature[:20]}...")
    print(f"[Body]: {body[:200]}")

    if not verify_signature(body.encode(), signature):
        print("[錯誤] 簽名驗證失敗")
        abort(400)

    events = json.loads(body).get("events", [])
    print(f"[事件數量]: {len(events)}")
    for event in events:
        print(f"[事件類型]: {event['type']}")
        if event["type"] in ("follow", "join"):
            reply_token = event["replyToken"]
            reply_message(reply_token, "我是小星空 ⭐\n💰 理財  🌿 生活  💬 聊天\n什麼都可以問我！")
        elif event["type"] == "message" and event["message"]["type"] == "text":
            user_id = event["source"].get("userId", "unknown")
            user_message = event["message"]["text"]
            reply_token = event["replyToken"]
            print(f"[用戶訊息]: {user_message}")
            ai_response = ask_ai(user_id, user_message)
            print(f"[AI回覆]: {ai_response[:80]}")
            reply_message(reply_token, ai_response)
            _write_status("line_bot", "已回覆訊息", {"messages_today": _bump_messages_today()})

    return "OK"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print(f"小星空啟動中... port={port}")
    app.run(host="0.0.0.0", port=port)
