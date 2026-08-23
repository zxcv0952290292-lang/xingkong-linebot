#!/usr/bin/env python3
"""LINE Bot 小星空 — 雲端部署版（Fly.io）"""
from flask import Flask, request, abort, jsonify
import requests
import json
import hashlib
import hmac
import base64
import os
import re
import math
import concurrent.futures
import uuid
import time
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta
os.environ.setdefault("TENANT_SCOPE", "1")  # 雲端沒有 .env，租戶隔離開關用環境變數
import supa  # Supabase 對話記憶（重啟不忘）
import inbox as m1  # M1 訊息與名單：非主人的訊息走規則自動回（設定在 tenants.config）

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
# 擺渡人 OA（@078jnspi）——對客的帳號。兩把鑰匙放 Render 環境變數；沒設＝這條路關著（404）。
FERRYMAN_CHANNEL_SECRET = os.environ.get("FERRYMAN_CHANNEL_SECRET", "")
FERRYMAN_CHANNEL_TOKEN = os.environ.get("FERRYMAN_CHANNEL_TOKEN", "")
OWNER_UID = os.environ.get("OWNER_UID", "U6d485aa77b4a6779f61ad7c263e43d65")

# ── 星空信箱（2026-08-23）────────────────────────────────────
# Stanley 2026-07-12 在 LINE 上問過「你能幫我轉達給終端的星空嗎」，當時答不行。
# 其實管線都在了，缺的只是一個標記：主人打「星空 ……」開頭，就不走 AI，
# 直接投進 job_queue（kind=cc_inbox），終端機那邊的 Claude Code 自己來拿。
# 附件（圖／語音／影片）只記 messageId，本機拿 channel token 自己去 LINE 抓原檔
# ——webhook 不下載，才不會卡住（LINE 逾時會重送）。
CC_PREFIX = "星空"


def cc_put(text: str, media: str = "text", message_id: str = "") -> int:
    """投一封進星空信箱。回傳投完之後信箱裡的未讀數（回覆時告訴他堆了幾封）。"""
    # ⚠️ Render 跑在 UTC，datetime.now() 不帶時區的話存進去是 UTC，
    # 而本機取件與中樞顯示都用台灣時間——2026-08-23 第一次實測就看到
    # 「18:04 傳的訊息顯示 10:04」。整條鏈路一律存台灣時間。
    now = (datetime.utcnow() + timedelta(hours=8)).isoformat(timespec="seconds")
    supa.insert("job_queue", [{
        "id": str(uuid.uuid4()), "tenant_id": "stanley",
        "kind": "cc_inbox", "status": "pending",
        "payload": {"text": text, "media": media, "message_id": message_id,
                    "from": "line", "at": now},
    }])
    try:
        rows = supa.select("job_queue",
                           "status=eq.pending&kind=eq.cc_inbox&select=id")
        return len(rows or [])
    except Exception:
        return 0
SCAN_TOKEN = os.environ.get("SCAN_TOKEN", "")  # 保護 /tasks/scan_alerts
BASE = os.path.dirname(os.path.abspath(__file__))
LAST_PUSH_FILE = os.path.join(BASE, "last_stock_push.json")


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

_PUSH_CACHE = {"at": 0, "data": None}


def load_last_push():
    """讀今日推播內容。

    2026-08-22 修正：原本只讀本機的 last_stock_push.json，但這支跑在 Render、
    寫檔的 twstock_bot 跑在 Stanley 的 Mac —— **兩台不同機器**，而且
    line_bot_deploy/ 裡根本沒有那個檔，所以「問今日推播」這功能從來沒運作過。
    改成優先讀 Supabase（twstock_bot 現在會同時寫進去），本機檔案當備援。

    快取 5 分鐘，避免每則訊息都打一次資料庫。
    """
    import time as _t
    if _PUSH_CACHE["data"] and _t.time() - _PUSH_CACHE["at"] < 300:
        return _PUSH_CACHE["data"]

    data = None
    try:
        rows = supa.select("module_status",
                           "module_name=eq.stock_last_push&select=detail")
        if rows:
            data = rows[0].get("detail")
    except Exception as e:
        print(f"[load_last_push] Supabase 讀取失敗：{e}")

    if not data:                      # 備援：本機檔（本機執行時才會有）
        try:
            with open(LAST_PUSH_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = None

    _PUSH_CACHE.update({"at": _t.time(), "data": data})
    return data

def verify_signature(body, signature, secret=None):
    hash = hmac.new((secret or LINE_CHANNEL_SECRET).encode(), body, hashlib.sha256).digest()
    return base64.b64encode(hash).decode() == signature

def build_system_prompt():
    push = load_last_push()
    if push:
        return SYSTEM_PROMPT + f"\n\n【今日推播（{push.get('date','')}）】\n{push.get('content','')[:800]}"
    return SYSTEM_PROMPT

BRAIN_ASLEEP_MSG = "小星空的大腦在家裡睡覺 😴\n（Stanley 的 Mac 沒醒，AI 回話要靠它）\n行情、到價提醒這些還是可以用喔 ⭐"

def brain_awake():
    """大腦心跳 90 秒內才算醒著。brain_daemon 每次迴圈會更新 brain_heartbeat。
    先判斷再建單——不然工單丟進去也沒人撈，使用者要枯等 60 秒才拿到誤導的『正在忙』。"""
    try:
        rows = supa.select("module_status", "module_name=eq.brain_heartbeat&select=last_run")
        if not rows:
            return False
        ts = (rows[0].get("last_run") or "").replace("Z", "+00:00")
        last = datetime.fromisoformat(ts)
        now = datetime.now(last.tzinfo) if last.tzinfo else datetime.now()
        return (now - last).total_seconds() < 90
    except Exception as e:
        print(f"[brain_awake err] {e}")
        return True          # 判斷不出來就照舊流程走，不要因為這個擋掉功能

def ask_ai(user_id, user_message):
    """走本機大腦佇列（Supabase 工單 → Mac brain_daemon 用 Claude 訂閱額度跑），
    不再打 Anthropic API（API 帳戶餘額歸零）。代價：Mac 要醒著、回話約 10-40 秒。"""
    if not brain_awake():
        return BRAIN_ASLEEP_MSG
    try:
        history = load_history(user_id)
        convo = "\n".join(
            f"{'用戶' if m.get('role') == 'user' else '小星空'}：{m.get('content','')}"
            for m in history)
        prompt = (build_system_prompt()
                  + (f"\n\n【最近對話】\n{convo}" if convo else "")
                  + f"\n\n用戶剛剛說：{user_message}\n\n"
                  + "請用小星空的語氣回一則 LINE 訊息（繁體中文、100 字內、可用 emoji），只輸出訊息本身。")
        tid = "ticket:" + str(uuid.uuid4())
        now = datetime.now().isoformat()
        ok = supa.insert("module_status", [{
            "module_name": tid, "last_run": now, "status": "pending",
            "detail": {"status": "pending", "prompt": prompt, "speed": "fast", "created": now}}])
        if not ok:
            return "抱歉，我現在連不上大腦，等等再問我 😅"
        for _ in range(30):                       # 最多輪詢 ~60 秒
            time.sleep(2)
            rows = supa.select("module_status", f"module_name=eq.{tid}&select=detail")
            d = (rows[0].get("detail") if rows else {}) or {}
            if d.get("status") == "done":
                reply = (d.get("result") or "").strip()
                save_turn(user_id, user_message, reply)
                return reply or "嗯…我剛剛恍神了，再問我一次好嗎 😅"
            if d.get("status") == "error":
                break
        # 輪詢逾時：可能是大腦中途睡著，也可能真的塞車——分開講，不要一律說「正在忙」
        return BRAIN_ASLEEP_MSG if not brain_awake() else "小星空的大腦正在忙，等我一下下再問我一次好嗎 😅"
    except Exception as e:
        notify_owner(f"⚠️ 小星空出錯：{e}")
        return "抱歉，我現在有點問題，請稍後再試 😅"

# ── M1 訊息與名單：規則設定從 tenants.config 讀（tenant.py --push 推上來的）──
_INBOX_CACHE = {"at": 0, "cfg": None}

def _inbox_cfg():
    """讀 M1 規則設定，快取 5 分鐘。讀不到回 None（呼叫端要有備援）。"""
    if _INBOX_CACHE["cfg"] and time.time() - _INBOX_CACHE["at"] < 300:
        return _INBOX_CACHE["cfg"]
    try:
        rows = supa.select("tenants", "id=eq.stanley&select=config", tenant="*")
        cfg = ((rows[0].get("config") or {}).get("inbox")) if rows else None
        if cfg and cfg.get("rules") is not None:
            _INBOX_CACHE.update({"at": time.time(), "cfg": cfg})
            return cfg
    except Exception as e:
        print(f"[inbox cfg err] {e}")
    return _INBOX_CACHE["cfg"]  # 讀失敗就用上一次的，總比沒有好

def _line_profile_name(uid, token=None):
    """抓 LINE 顯示名稱，抓不到回空字串（不擋流程）。uid 是頻道限定的，要用對的鑰匙查。"""
    try:
        r = requests.get(f"https://api.line.me/v2/bot/profile/{uid}",
                         headers={"Authorization": f"Bearer {token or LINE_CHANNEL_TOKEN}"}, timeout=5)
        if r.status_code == 200:
            return r.json().get("displayName", "")
    except Exception:
        pass
    return ""

def m1_handle(user_id, user_message, reply_token, token=None):
    """訪客訊息：接住→認人→分類→自動回→記錄；該人工的推播叫主人。
    token＝這則訊息來自哪個 OA 就用哪把鑰匙回（None＝小星空）。通知主人永遠走小星空。"""
    cfg = _inbox_cfg()
    if not cfg:
        reply_message(reply_token, "收到你的訊息了，我們會盡快回覆你。", token=token)
        notify_owner(f"🔔 有訪客訊息（M1 設定讀不到，先用備援回覆）\n訪客說：{user_message[:80]}")
        return
    try:
        r = m1.handle("line", user_id, user_message,
                      name=_line_profile_name(user_id, token=token),
                      tid="stanley", cfg=cfg, quiet=True)
    except Exception as e:
        print(f"[m1 err] {e}")
        reply_message(reply_token, "收到你的訊息了，我們會盡快回覆你。", token=token)
        notify_owner(f"⚠️ M1 出錯：{e}\n訪客說：{user_message[:80]}")
        return
    if r.get("reply"):
        reply_message(reply_token, r["reply"], token=token)
    if r.get("needs_human"):
        notify_owner(f"🔔 要人工接手\n訪客說：{user_message[:80]}\n（規則：{r.get('rule') or '沒命中'}）")

def notify_owner(msg):
    try:
        requests.post("https://api.line.me/v2/bot/message/push",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_TOKEN}"},
            json={"to": OWNER_UID, "messages": [{"type": "text", "text": msg}]}, timeout=10)
    except:
        pass

def reply_message(reply_token, text, token=None):
    res = requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token or LINE_CHANNEL_TOKEN}"},
        json={"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}
    )
    print(f"[LINE回覆狀態]: {res.status_code} {res.text}")

def push_message(uid, text):
    """主動推播給任一使用者（到價提醒用）。"""
    try:
        requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_TOKEN}"},
            json={"to": uid, "messages": [{"type": "text", "text": text}]}, timeout=10)
    except Exception as e:
        print(f"[push err] {e}")

# ══════════════════════════════════════════════════════════════
#  股票到價提醒（#10）— LINE 打字設定，盤中每 5 分外部 cron 掃描
#  存於 module_status（免 DDL）：module = palert:{uid}:{code}
# ══════════════════════════════════════════════════════════════
ALERT_PREFIX = "palert"

def _now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

def _alert_key(uid, code):
    return f"{ALERT_PREFIX}:{uid}:{code}"

def _parse_alert(text):
    """解析到價指令 → {'code','target','direction'} 或 None。
    例：'2330 到 600'、'2330 漲到 600'、'2330 跌破 550'、'2330>600'、'提醒我 2330 到 600'
    direction: 'above' / 'below' / None(依現價推斷)"""
    t = text.strip().replace("　", " ")
    m = re.search(
        r"(\d{3,6})\D*?(漲到|跌到|跌破|突破|站上|高於|低於|大於|小於|以上|以下|到|破|≥|≤|>|<|=)\s*(\d+(?:\.\d+)?)",
        t)
    if not m:
        return None
    code, kw, price = m.group(1), m.group(2), float(m.group(3))
    above_kw = ("漲到", "突破", "站上", "高於", "大於", "以上", "破", "≥", ">")
    below_kw = ("跌到", "跌破", "低於", "小於", "以下", "≤", "<")
    direction = "above" if kw in above_kw else ("below" if kw in below_kw else None)
    return {"code": code, "target": price, "direction": direction}

def set_alert(uid, code, target, direction):
    """設定到價提醒。回 dict 或 None（代號查無）。"""
    info, _ = _yahoo_quote(code)
    if not info:
        return None
    cur = info["price"]
    name = STOCK_NAMES.get(code, code)
    if direction is None:
        direction = "above" if target >= cur else "below"
    supa.upsert("module_status", [{
        "module_name": _alert_key(uid, code),
        "last_run": _now_iso(),
        "status": "active",
        "detail": {"user_id": uid, "code": code, "name": name, "target": target,
                   "direction": direction, "active": True,
                   "created": _now_iso(), "set_price": cur},
    }], "module_name")
    return {"name": name, "cur": cur, "target": target, "direction": direction}

def list_alerts(uid):
    rows = supa.select("module_status", f"module_name=like.{ALERT_PREFIX}:{uid}:*&select=detail")
    return [r["detail"] for r in rows if (r.get("detail") or {}).get("active")]

def cancel_alert(uid, code):
    rows = supa.select("module_status", f"module_name=eq.{_alert_key(uid, code)}&select=detail")
    if not rows or not (rows[0].get("detail") or {}).get("active"):
        return False
    ex = rows[0]["detail"]
    ex["active"] = False
    supa.upsert("module_status", [{"module_name": _alert_key(uid, code), "last_run": _now_iso(),
                                    "status": "cancelled", "detail": ex}], "module_name")
    return True

def handle_alert_command(uid, text):
    """是到價相關指令就處理並回字串；否則回 None（交給 AI）。"""
    t = text.strip()
    if t in ("到價清單", "我的到價", "到價", "查到價", "提醒清單", "到價提醒"):
        alerts = list_alerts(uid)
        if not alerts:
            return "你目前沒有設定到價提醒。\n\n設定方式：輸入「2330 到 600」\n就會在台積電到 600 時通知你 🔔"
        lines = ["🔔 你的到價提醒："]
        for ex in alerts:
            arrow = "漲到" if ex["direction"] == "above" else "跌到"
            lines.append(f"・{ex['name']}（{ex['code']}）{arrow} {ex['target']}")
        lines.append("\n刪除：輸入「刪 2330」")
        return "\n".join(lines)
    m = re.match(r"^(刪|刪除|取消|移除)\s*(\d{3,6})", t)
    if m:
        ok = cancel_alert(uid, m.group(2))
        return f"已刪除 {m.group(2)} 的到價提醒 ✅" if ok else f"找不到 {m.group(2)} 的到價提醒 🤔"
    parsed = _parse_alert(t)
    if parsed:
        r = set_alert(uid, parsed["code"], parsed["target"], parsed["direction"])
        if not r:
            return f"找不到股票 {parsed['code']}，請確認代號正確 🤔"
        arrow = "漲到" if r["direction"] == "above" else "跌到"
        return (f"✅ 到價提醒設定完成\n"
                f"{r['name']}（{parsed['code']}）目前 {r['cur']}\n"
                f"當它{arrow} {r['target']} 就通知你 🔔\n\n"
                f"（台股盤中每 5 分鐘檢查；輸入「到價清單」可查看）")
    return None

def _market_open():
    """台股盤中：週一至週五 09:00–13:35（台灣時間，Render 跑 UTC）。"""
    now = datetime.utcnow() + timedelta(hours=8)
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return 9 * 60 <= hm <= 13 * 60 + 35

def scan_alerts():
    """掃一次所有 active 到價提醒，到價就 push 並標記 fired（idempotent）。"""
    rows = supa.select("module_status", f"module_name=like.{ALERT_PREFIX}:*&select=module_name,detail")
    active = [(r["module_name"], r["detail"]) for r in rows if (r.get("detail") or {}).get("active")]
    if not active:
        return {"checked": 0, "fired": 0}
    by_code = {}
    for module, ex in active:
        by_code.setdefault(ex["code"], []).append((module, ex))
    fired = 0
    for code, items in by_code.items():
        info, _ = _yahoo_quote(code)
        if not info:
            continue
        price = info["price"]
        for module, ex in items:
            hit = price >= ex["target"] if ex["direction"] == "above" else price <= ex["target"]
            if not hit:
                continue
            ex2 = dict(ex); ex2["active"] = False
            ex2["fired_price"] = price; ex2["fired_at"] = _now_iso()
            supa.upsert("module_status", [{"module_name": module, "last_run": _now_iso(),
                                            "status": "fired", "detail": ex2}], "module_name")
            arrow = "漲到" if ex["direction"] == "above" else "跌到"
            push_message(ex["user_id"],
                         f"🔔 到價提醒觸發\n{ex['name']}（{code}）現在 {price}\n"
                         f"已{arrow}你設定的 {ex['target']} ⭐\n\n輸入代號可查即時分析")
            fired += 1
    return {"checked": len(active), "fired": fired}

# ══════════════════════════════════════════════════════════════
#  自選股即時分析 API
#  （原本在 Netlify function，但每日 zip 部署不會 bundle functions →
#   /api/analyze 一直 404。移到這個 Render 持久服務就不會被洗掉。）
# ══════════════════════════════════════════════════════════════
STOCK_UA = "Mozilla/5.0"

# 台股代號→中文名對照表（打包成靜態檔，Render 離線也查得到）
try:
    with open(os.path.join(BASE, "stock_names.json"), encoding="utf-8") as _f:
        STOCK_NAMES = json.load(_f)
    print(f"[stock_names] 載入 {len(STOCK_NAMES)} 檔中文名")
except Exception as _e:
    STOCK_NAMES = {}
    print(f"[stock_names] 載入失敗: {_e}")


def _yahoo_quote(code):
    """一次 Yahoo chart 請求同時取得報價與技術指標（省一次網路來回；Yahoo 全球可存取）。
    回傳 (info, kline)。找不到回 (None, None)。"""
    for suf, exch in ((".TW", "上市"), (".TWO", "上櫃")):
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{code}{suf}",
                params={"interval": "1d", "range": "6mo"},
                headers={"User-Agent": STOCK_UA}, timeout=10)
            result = ((r.json().get("chart") or {}).get("result") or [None])[0]
            if not result:
                continue
            meta = result.get("meta") or {}
            price = meta.get("regularMarketPrice")
            if not price:
                continue
            kl = _kline_from_result(result)
            # 昨收：用 K 線倒數第二根收盤最準（Yahoo meta 的 previousClose 在長區間會抓到區間起點）
            prev = (kl or {}).get("_prev_close") or meta.get("previousClose") or 0
            info = {
                "name": code,  # 中文名稍後由對照表覆蓋
                "price": float(price),
                "prev": float(prev or 0),
                "exchange": exch,
            }
            return info, kl
        except Exception:
            pass
    return None, None

def _kline_from_result(result):
    """從 Yahoo chart result 算技術指標（MA/RSI/MACD/布林/支撐壓力/量比）。"""
    try:
        q = result["indicators"]["quote"][0]
        ts = result.get("timestamp") or []
        closes, highs, lows, vols = [], [], [], []
        for i in range(len(ts)):
            c, h, l, v = q["close"][i], q["high"][i], q["low"][i], q["volume"][i]
            if c and h and l and v:
                closes.append(c); highs.append(h); lows.append(l); vols.append(v / 1000)
        n = len(closes)
        if n < 5:
            return None
        def avg(arr, length):
            return sum(arr[-length:]) / min(length, len(arr))
        def ema(arr, p):
            k = 2 / (p + 1); e = arr[0]
            for x in arr[1:]:
                e = x * k + e * (1 - k)
            return round(e, 2)
        ma5, ma10, ma20 = round(avg(closes, 5), 1), round(avg(closes, 10), 1), round(avg(closes, 20), 1)
        macd = round(ema(closes, 12) - ema(closes, 26), 2) if n >= 26 else 0
        gains, losses = [], []
        for i in range(1, min(15, n)):
            diff = closes[n - i] - closes[n - i - 1]
            (gains if diff > 0 else losses).append(abs(diff))
        ag = (sum(gains) / 14) or 0.01
        al = (sum(losses) / 14) or 0.01
        rsi = round(100 - 100 / (1 + ag / al), 1)
        std20 = math.sqrt(sum((v - ma20) ** 2 for v in closes[-20:]) / min(20, n))
        avg_vol = sum(vols[-20:]) / min(20, n)
        return {
            "ma5": ma5, "ma10": ma10, "ma20": ma20, "macd": macd, "rsi": rsi,
            "bollUp": round(ma20 + 2 * std20, 1), "bollDown": round(ma20 - 2 * std20, 1),
            "support": round(min(lows[-20:]), 1), "resistance": round(max(highs[-20:]), 1),
            "volRatio": round(vols[-1] / avg_vol, 1) if avg_vol > 0 else 1,
            "latestVol": round(vols[-1]),
            "_prev_close": closes[-2] if n >= 2 else None,
        }
    except Exception:
        return None



def _api_cors(resp):
    if request.path.startswith("/api/"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

@app.route("/api/analyze")
def api_analyze():
    code = (request.args.get("code") or "").strip()
    if not code:
        return jsonify({"error": "請提供股票代號"}), 400
    # 2026-08-23：原本三路並行（報價／籌碼／基本面），但後兩路走 FinMind，
    # 從 Render 機房打出去被限流，回來永遠是 null（實測 2603、2330 都一樣）。
    # 兩路死的併發沒有意義，直接拿掉——端點也從 3 個外部請求降成 1 個。
    info, kl = _yahoo_quote(code)

    if not info:
        return jsonify({"error": f"找不到 {code}，請確認代號正確"}), 404
    # 中文名（打包對照表）
    info["name"] = STOCK_NAMES.get(code, code)

    prev = info.get("prev") or 0
    chg_pct = round((info["price"] - prev) / prev * 100, 2) if prev else 0
    # 這三段（k_text / chip_text / fund_text）原本只是用來組 AI 的 prompt，
    # prompt 拿掉之後就成了孤兒，而且會參照已刪除的 chip / fund → NameError。

    # 2026-08-23：這裡原本把上面組好的資料丟給 Anthropic API 產出
    # story / technical / chip / fundamental / suggestion / entry / stop_loss…
    # 兩個問題：
    #   ① 那個 API 帳戶餘額歸零（全系統早已改走本機訂閱腦），呼叫必定失敗，
    #      而且失敗被 try 吞掉 → 回傳缺欄位 → 網頁那四格永遠印「–」。
    #   ② 它產的是進場價／停損停利／操作建議，屬於投資建議；
    #      Stanley 的保險業務員登錄尚未註銷，那是合規紅線。
    # 現在網頁自己用數字組出事實描述（見 cf_stock_new 的 sayTech/sayChip/sayFund），
    # 籌碼與基本面走 /api/stock（自家倉庫＋交易所正本），這支只負責報價與 K 線。

    return jsonify({
        "code": code, "name": info["name"], "exchange": info["exchange"],
        "price": info["price"], "change_pct": chg_pct,
        "kline": kl,
        "note": "籌碼與基本面請走 ferryman-stock 的 /api/stock；本端點只提供報價與 K 線。",
    })

@app.route("/api/vote", methods=["GET", "POST"])
def api_vote():
    """每日推播的看多/看空投票，累計存 Supabase。"""
    raw = (request.args.get("vote") or "").strip().lower()
    vote = {"bull": "up", "bear": "down", "up": "up", "down": "down"}.get(raw)
    date = (request.args.get("date") or datetime.now().strftime("%Y-%m-%d")).replace("/", "-")
    if not vote:
        return jsonify({"error": "vote 只能是 up/down/bull/bear"}), 400
    if not supa.enabled():
        return jsonify({"ok": False, "reason": "db 未設定"})
    key = f"stock_vote_{date}"
    rows = supa.select("module_status", f"module_name=eq.{key}&select=detail")
    cur = (rows[0].get("detail") if rows else None) or {}
    cur[vote] = int(cur.get(vote, 0)) + 1
    supa.upsert("module_status", [{
        "module_name": key,
        "last_run": datetime.now().strftime("%Y-%m-%dT%H:%M:%S") + "+08:00",
        "status": "vote", "detail": cur,
    }], "module_name")
    return jsonify({"ok": True, "up": cur.get("up", 0), "down": cur.get("down", 0)})

@app.route("/api/backtest")
def api_backtest():
    """歷史推薦戰績回測，彙總 push_history 已回填的 return_pct。"""
    if not supa.enabled():
        return jsonify({"samples": 0, "error": "db 未設定"})
    rows = supa.select("push_history",
        "select=push_date,code,name,return_pct&return_pct=not.is.null&order=push_date.desc&limit=2000")
    rets = [r["return_pct"] for r in rows if r.get("return_pct") is not None]
    n = len(rets)
    if n == 0:
        return jsonify({"samples": 0})
    wins = sum(1 for x in rets if x > 0)
    dates = sorted(set(r["push_date"] for r in rows))
    best = max(rows, key=lambda r: r["return_pct"])
    worst = min(rows, key=lambda r: r["return_pct"])
    buckets = {"lt-10": 0, "n10-0": 0, "p0-10": 0, "p10-20": 0, "gt20": 0}
    for x in rets:
        if x < -10: buckets["lt-10"] += 1
        elif x < 0: buckets["n10-0"] += 1
        elif x < 10: buckets["p0-10"] += 1
        elif x < 20: buckets["p10-20"] += 1
        else: buckets["gt20"] += 1
    return jsonify({
        "samples": n, "days": len(dates),
        "date_from": dates[0], "date_to": dates[-1],
        "win_rate": round(wins / n * 100, 1),
        "avg_return": round(sum(rets) / n, 2),
        "best": {"name": best.get("name") or best["code"], "code": best["code"], "ret": best["return_pct"]},
        "worst": {"name": worst.get("name") or worst["code"], "code": worst["code"], "ret": worst["return_pct"]},
        "buckets": buckets,
        "recent": [{"date": r["push_date"], "name": r.get("name") or r["code"],
                    "code": r["code"], "ret": r["return_pct"]} for r in rows[:12]],
        "all": [{"date": r["push_date"], "name": r.get("name") or r["code"],
                 "code": r["code"], "ret": r["return_pct"]} for r in rows],
    })

@app.route("/tasks/scan_alerts", methods=["GET", "POST"])
def task_scan_alerts():
    """外部 cron（GitHub Actions 每 5 分）呼叫 → 掃一次到價提醒。"""
    token = request.args.get("token") or request.headers.get("X-Scan-Token", "")
    if SCAN_TOKEN and token != SCAN_TOKEN:
        abort(403)
    if not supa.enabled():
        return jsonify({"error": "db off"}), 503
    if not _market_open() and not request.args.get("force"):
        return jsonify({"skipped": "market_closed"})
    result = scan_alerts()
    print(f"[scan_alerts] {result}")
    return jsonify(result)

@app.route("/")
def index():
    return "小星空 online ⭐", 200

@app.route("/health")
def health():
    return "OK", 200

@app.route("/version")
def version():
    return "2026-08-23-ferryman-oa", 200

@app.route("/webhook/ferryman", methods=["POST"])
def webhook_ferryman():
    """擺渡人 OA（@078jnspi）——純對客帳號：所有訊息一律走 M1，這裡沒有小星空。
    鑰匙沒設就當這條路不存在（404），部署了也不會誤接。"""
    if not FERRYMAN_CHANNEL_SECRET or not FERRYMAN_CHANNEL_TOKEN:
        abort(404)
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    if not verify_signature(body.encode(), signature, secret=FERRYMAN_CHANNEL_SECRET):
        print("[ferryman] 簽名驗證失敗")
        abort(400)
    for event in json.loads(body).get("events", []):
        if event["type"] == "follow":
            reply_message(event["replyToken"],
                          "嗨，我是擺渡人。\n想了解存錢挑戰，回「報名」就可以。\n其他事慢慢說，我都會看到。",
                          token=FERRYMAN_CHANNEL_TOKEN)
        elif event["type"] == "message" and event["message"]["type"] == "text":
            m1_handle(event["source"].get("userId", "unknown"),
                      event["message"]["text"], event["replyToken"],
                      token=FERRYMAN_CHANNEL_TOKEN)
            _write_status("line_bot", "擺渡人OA已回覆", {"messages_today": _bump_messages_today()})
    return "OK"

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
            # 非主人 → M1 訊息與名單（規則自動回；小星空只服務主人）
            if user_id != OWNER_UID:
                m1_handle(user_id, user_message, reply_token)
                _write_status("line_bot", "已回覆訊息", {"messages_today": _bump_messages_today()})
                continue
            # 「星空 ……」＝要給終端機那邊的 Claude Code，不走 AI、不花額度
            if user_message.startswith(CC_PREFIX):
                body_text = user_message[len(CC_PREFIX):].strip(" 　:：,，")
                if body_text:
                    try:
                        n = cc_put(body_text)
                        reply_message(reply_token,
                                      f"📮 收到，已放進星空的信箱（未讀 {n} 封）\n"
                                      f"他在電腦前的話會馬上看到；沒開機就等下次開工。")
                    except Exception as e:
                        print(f"[cc_put err] {e}")
                        reply_message(reply_token, "📮 信箱寫不進去，等等再試一次 😅")
                    _write_status("line_bot", "已回覆訊息",
                                  {"messages_today": _bump_messages_today()})
                    continue

            # 先看是不是到價提醒指令，是就直接回、不走 AI
            try:
                alert_reply = handle_alert_command(user_id, user_message) if supa.enabled() else None
            except Exception as e:
                print(f"[alert err] {e}")
                alert_reply = None
            if alert_reply is not None:
                reply_message(reply_token, alert_reply)
            else:
                # AI 走本機大腦佇列（10-40s），不能卡住 webhook（LINE 會逾時重送）。
                # 先秒回一句「思考中」用 reply，真答案在背景算完用 push 送。
                reply_message(reply_token, "小星空想一下喔 🌟")
                def _bg(uid, msg):
                    try:
                        ans = ask_ai(uid, msg)
                        print(f"[AI回覆]: {ans[:80]}")
                        push_message(uid, ans)
                    except Exception as e:
                        print(f"[bg err] {e}")
                        push_message(uid, "抱歉，我剛剛卡住了，再問我一次好嗎 😅")
                threading.Thread(target=_bg, args=(user_id, user_message), daemon=True).start()
            _write_status("line_bot", "已回覆訊息", {"messages_today": _bump_messages_today()})

        # 主人傳的圖／語音／影片一律進星空信箱。
        # 在這之前這些訊息是**整個被忽略**的（只有 type=="text" 進得來），
        # 所以這裡沒有搶走任何原本的行為。
        elif (event["type"] == "message"
              and event["message"]["type"] in ("image", "audio", "video", "file")
              and event["source"].get("userId") == OWNER_UID):
            mtype = event["message"]["type"]
            mid = event["message"]["id"]
            NAME = {"image": "圖片", "audio": "語音", "video": "影片", "file": "檔案"}
            try:
                n = cc_put("", media=mtype, message_id=mid)
                extra = "（會自動轉成文字）" if mtype == "audio" else ""
                reply_message(event["replyToken"],
                              f"📮 {NAME[mtype]}收到了{extra}，已放進星空的信箱（未讀 {n} 封）\n"
                              f"⚠️ LINE 的原檔不會永久保存，他太久沒開機的話附件可能會過期。")
            except Exception as e:
                print(f"[cc_put media err] {e}")
                reply_message(event["replyToken"], "📮 附件收不進信箱，等等再試 😅")
            _write_status("line_bot", "已回覆訊息", {"messages_today": _bump_messages_today()})

    return "OK"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print(f"小星空啟動中... port={port}")
    app.run(host="0.0.0.0", port=port)
