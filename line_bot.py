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
import line_keys  # 一家店的 LINE 鑰匙放哪裡（env 優先，其次雲端加密欄位）

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

# ── FB 粉專（Messenger Platform）──────────────────────────────
# 2026-08-26 他要粉專私訊也自動回。Messenger **有 webhook**，所以是即時的，
# 不像 IG／Threads 私訊只能輪詢（Meta 不給個人帳號 webhook）。
# 三把鑰匙都要有，缺一就當這條路不存在（404），跟 ferryman 同一個保護。
FB_PAGE_TOKEN = os.environ.get("FB_PAGE_TOKEN", "")      # 粉專存取權杖
FB_VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN", "")  # 掛 webhook 時 Meta 會來對這個字串
FB_APP_SECRET = os.environ.get("FB_APP_SECRET", "")      # 驗簽（確認真的來自 Meta）
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
# ── 順風車：本機推不出去的回覆，搭他下一則訊息免費送（2026-08-28）──────
#
# 這支 OA 是免費方案，**主動推播一個月只有 200 則**，08-28 當天用完，
# 之後每一次 push 都是 429。他那邊看到的是：訊息傳進去了、沒有任何回音
# ——他以為是「電腦開機後沒去讀取」，其實讀了，只是回不了。
#
# 但 LINE 的**回覆**（replyToken）不算額度、沒有上限。所以只要他再打一個字，
# 就把積著的話夾在那一則回覆裡一起送出去，一毛額度都不花。
#
# ⚠️ 一次最多夾 4 則：reply 一次上限 5 則，要留一個位子給本來要回的那句。
CC_OUT = "line_pending"


def cc_take_pending(limit: int = 4) -> list:
    """撈出待送的回覆並標成已送。撈不到就回空陣列，絕不讓它把 webhook 弄壞。"""
    try:
        rows = supa.select("job_queue",
                           f"status=eq.pending&kind=eq.{CC_OUT}"
                           f"&select=id,payload&order=created_at&limit={limit}")
    except Exception as e:
        print(f"[carry err] {e}")
        return []
    out = []
    now = (datetime.utcnow() + timedelta(hours=8)).isoformat(timespec="seconds")
    for r in rows or []:
        t = ((r.get("payload") or {}).get("text") or "").strip()
        if not t:
            continue
        out.append(t[:4900])
        try:
            supa.update("job_queue", f"id=eq.{r['id']}",
                        {"status": "done", "finished_at": now,
                         "result": {"text": "搭 reply 順風車送出"}})
        except Exception as e:
            print(f"[carry mark err] {e}")
    return out


def reply_owner(reply_token, text):
    """回主人。**先把積著送不出去的話夾進來**，再接本來要回的那一句。"""
    carry = cc_take_pending()
    if carry:
        print(f"[carry] 夾帶 {len(carry)} 則先前推不出去的訊息")
    reply_message(reply_token, carry + [text] if carry else text)


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
_TENANT_CACHE = {}      # tid → {"at","cfg","active"}


def _tenant_cfg(tid):
    """任何一個租戶的 M1 設定，快取 5 分鐘。fail-open 規則跟 _inbox_cfg 一模一樣：
    **讀不到＝不知道，不是停權**——資料庫抽風不能讓所有客戶的自動回覆一起啞掉。"""
    c = _TENANT_CACHE.get(tid)
    if c and time.time() - c["at"] < 300:
        return c["cfg"], c["active"]
    try:
        rows = supa.select("tenants", f"id=eq.{tid}&select=config,status", tenant="*")
        if rows:
            conf = rows[0].get("config") or {}
            cfg = conf.get("inbox")
            ov = conf.get("owner_vars") or {}
            if cfg and ov:
                cfg = dict(cfg, vars={**(cfg.get("vars") or {}), **ov})
            active = (rows[0].get("status") or "active") == "active"
            if cfg and cfg.get("rules") is not None:
                _TENANT_CACHE[tid] = {"at": time.time(), "cfg": cfg, "active": active}
                return cfg, active
            if not active:
                _TENANT_CACHE[tid] = {"at": time.time(), "cfg": cfg, "active": False}
                return cfg, False
    except Exception as e:
        print(f"[tenant cfg err {tid}] {e}")
    return (c or {}).get("cfg"), (c or {}).get("active", True)


def _inbox_cfg():
    """讀 M1 規則設定，快取 5 分鐘。讀不到回 None（呼叫端要有備援）。

    **停權的租戶不服務**：billing.py 把 status 改成 suspended 之後，
    這裡讀不到 active 的那筆 → 回 (None, False)，自動回覆整個停掉、
    只留通知主人。沒繳錢卻照跑，計費就是假的。
    """
    if _INBOX_CACHE["cfg"] and time.time() - _INBOX_CACHE["at"] < 300:
        return _INBOX_CACHE["cfg"], _INBOX_CACHE.get("active", True)
    try:
        rows = supa.select("tenants", "id=eq.stanley&select=config,status", tenant="*")
        # ⚠️ 讀失敗時 supa.select 回 []，那是「不知道」不是「停權」。
        # 只有真的讀到一筆、而且它不是 active，才算停權。
        if rows:
            cfg = (rows[0].get("config") or {}).get("inbox")
            # 店主自己在中樞改的值（今天的狀況）住在 config.owner_vars，
            # 不在 inbox.vars——那段每次 tenant.py --push 都會被本機檔蓋掉。
            # 不在這裡疊回去，就是「中樞看得到、客人聽不到」。
            ov = (rows[0].get("config") or {}).get("owner_vars") or {}
            if cfg and ov:
                cfg = dict(cfg, vars={**(cfg.get("vars") or {}), **ov})
            if (rows[0].get("status") or "active") != "active":
                _INBOX_CACHE.update({"at": time.time(), "cfg": cfg, "active": False})
                return cfg, False
            if cfg and cfg.get("rules") is not None:
                _INBOX_CACHE.update({"at": time.time(), "cfg": cfg, "active": True})
                return cfg, True
    except Exception as e:
        print(f"[inbox cfg err] {e}")
    return _INBOX_CACHE["cfg"], _INBOX_CACHE.get("active", True)  # 讀失敗就用上一次的

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

# ── 知識層的雲端出口 ──────────────────────────────────────
# knowledge.py 不認識任何模型（ask 是注入的），本機注入 brain.ask、雲端注入這支。
# ⚠️ 沒有金鑰就回 None——整條路照舊走 fallback 叫人，不會壞掉也不會亂答。
# 模型用 KNOWLEDGE_MODEL 環境變數換；預設 haiku 是照 2026-08-26 估的成本
# （一家店月 500 則、48% 走這層 ≈ NT$15/月）。要更準就換 claude-sonnet-5。
KNOWLEDGE_MODEL = os.environ.get("KNOWLEDGE_MODEL", "claude-haiku-4-5")
_ANTHROPIC = None


def _knowledge_ask(prompt):
    """給 knowledge.answer 用的模型呼叫。**這支永遠不拋例外**——
    它掛在客人的即時回覆路徑上，模型抽風不能讓 webhook 跟著倒。"""
    global _ANTHROPIC
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return ""
    if _ANTHROPIC is None:
        import anthropic
        _ANTHROPIC = anthropic.Anthropic(api_key=key)
    r = _ANTHROPIC.messages.create(
        model=KNOWLEDGE_MODEL, max_tokens=512,
        messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in r.content if b.type == "text")


def _answerer(question, card):
    """規則答不出來時問知識層。沒金鑰／出任何錯 → None → 照舊叫人。"""
    if not card or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import knowledge
        return knowledge.answer(question, card, ask=_knowledge_ask)
    except Exception as e:
        print(f"[knowledge] {str(e)[:100]}")
        return None


def m1_handle(user_id, user_message, reply_token, token=None, tid="stanley"):
    """訪客訊息：接住→認人→分類→自動回→記錄；該人工的推播叫主人。
    token＝這則訊息來自哪個 OA 就用哪把鑰匙回（None＝小星空）。通知主人永遠走小星空。
    tid＝這則訊息屬於哪個租戶（一店一個 OA，設定與名單都要分開）。"""
    cfg, active = _tenant_cfg(tid) if tid != "stanley" else _inbox_cfg()
    if not active:
        notify_owner(f"⏸ 服務停權中，自動回覆沒有送出\n訪客說：{user_message[:80]}")
        return
    if not cfg:
        reply_message(reply_token, "收到你的訊息了，我們會盡快回覆你。", token=token)
        notify_owner(f"🔔 有訪客訊息（M1 設定讀不到，先用備援回覆）\n訪客說：{user_message[:80]}")
        return
    try:
        r = m1.handle("line", user_id, user_message,
                      name=_line_profile_name(user_id, token=token),
                      tid=tid, cfg=cfg, answerer=_answerer, quiet=True)
    except Exception as e:
        print(f"[m1 err] {e}")
        reply_message(reply_token, "收到你的訊息了，我們會盡快回覆你。", token=token)
        notify_owner(f"⚠️ M1 出錯：{e}\n訪客說：{user_message[:80]}")
        return
    if r.get("reply"):
        reply_message(reply_token, r["reply"], token=token)
    if r.get("needs_human"):
        why = r.get("unsure") or f"規則：{r.get('rule') or '沒命中'}"
        where = "" if tid == "stanley" else f"【{tid}】"
        notify_owner(f"🔔 {where}要人工接手\n訪客說：{user_message[:80]}\n（{why}）")

def notify_owner(msg):
    try:
        requests.post("https://api.line.me/v2/bot/message/push",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_TOKEN}"},
            json={"to": OWNER_UID, "messages": [{"type": "text", "text": msg}]}, timeout=10)
    except:
        pass

def reply_message(reply_token, text, token=None):
    """text 可以是字串，也可以是字串陣列（一次回多則，LINE 上限 5 則）。

    收到多則時 LINE 會照順序顯示，中間有間隔——比塞成一大段好讀，
    而且「分則」本身就是節奏（第一則講我是誰，第二則才給動作）。
    """
    texts = [text] if isinstance(text, str) else list(text)[:5]
    res = requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token or LINE_CHANNEL_TOKEN}"},
        json={"replyToken": reply_token, "messages": [{"type": "text", "text": t} for t in texts]}
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



# 2026-09-13：這個 @app.after_request 在 9ccf3c7（08-23 清死依賴）被連帶刪掉，之後 /api/analyze 一直沒有
# Access-Control-Allow-Origin，ferryman-stock 站上「自選股即時分析」從瀏覽器打過來全被 CORS 擋 → 「分析失敗」。
# curl／Python 打得通（不受 CORS 約束），所以一直沒被抓到。
@app.after_request
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
    return "2026-09-13-api-cors-back", 200

@app.route("/portal/push", methods=["POST"])
def portal_push():
    """客戶中樞的「直接回覆」通道（中樞 v3）。

    CF Pages 不放 LINE token（前端環境，洩漏面太大），所以中樞按「回覆」
    是打到這裡，由本服務用該租戶自己的 OA token 推播。
    互認：兩邊都有 SUPABASE_SERVICE_KEY，用它做 HMAC——不新增密鑰。
    副作用：回覆成功＝這位客人「等你回」自動清掉（回了就是處理了）。
    """
    body = request.get_data()
    sig = request.headers.get("X-Portal-Sig", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    want = hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
    if not key or not hmac.compare_digest(sig, want):
        abort(401)
    d = json.loads(body)
    tenant = d.get("tenant", ""); uid = d.get("uid", ""); text = (d.get("text") or "").strip()
    if not (tenant and uid and text) or len(text) > 1000:
        return jsonify({"error": "缺欄位或太長"}), 400
    # 租戶 → 他自己的 OA token。寧可失敗也不能拿別人的帳號替他發話。
    tok = FERRYMAN_CHANNEL_TOKEN if tenant == "stanley" else os.environ.get(f"LINE_TOKEN_{tenant.upper()}", "")
    if not tok:
        return jsonify({"error": f"租戶 {tenant} 的 LINE token 未設定，回覆要先開通"}), 422
    r = requests.post("https://api.line.me/v2/bot/message/push",
                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"},
                      json={"to": uid, "messages": [{"type": "text", "text": text}]}, timeout=10)
    if r.status_code >= 300:
        return jsonify({"error": f"LINE {r.status_code}: {r.text[:120]}"}), 502
    # 記台帳＋清這位客人的「等你回」
    try:
        rows = supa.select("contacts", f"channel_uid=eq.{uid}&select=id", tenant=tenant)
        cid = rows[0]["id"] if rows else None
        if cid:
            supa.insert("conversations", [{
                "contact_id": cid, "direction": "out", "channel": "line", "text": text,
                "matched_rule": "portal:manual", "needs_human": False}], tenant=tenant)
            supa.update("conversations",
                        f"contact_id=eq.{cid}&direction=eq.in&needs_human=eq.true",
                        {"needs_human": False}, tenant=tenant)
    except Exception as e:
        print(f"[portal_push 記帳失敗] {e}")
    return jsonify({"ok": True})


@app.route("/portal/richmenu", methods=["POST"])
def portal_richmenu():
    """圖文選單上架（開店 SOP 積木）：收圖＋格子設定，用該租戶自己的 token 走 LINE API
    三步：建選單→傳圖→設為預設。HMAC 驗證同 /portal/push。"""
    body = request.get_data()
    sig = request.headers.get("X-Portal-Sig", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not key or not hmac.compare_digest(sig, hmac.new(key.encode(), body, hashlib.sha256).hexdigest()):
        abort(401)
    d = json.loads(body)
    tenant = d.get("tenant", "")
    tok = FERRYMAN_CHANNEL_TOKEN if tenant == "stanley" else os.environ.get(f"LINE_TOKEN_{tenant.upper()}", "")
    if not tok:
        return jsonify({"error": f"租戶 {tenant} 的 LINE token 未設定"}), 422
    H = {"Authorization": f"Bearer {tok}"}
    # 先清同名舊選單（重複上架不堆垃圾）
    old = requests.get("https://api.line.me/v2/bot/richmenu/list", headers=H, timeout=10).json()
    for m in old.get("richmenus", []):
        if m.get("name") == d["menu"]["name"]:
            requests.delete(f"https://api.line.me/v2/bot/richmenu/{m['richMenuId']}", headers=H, timeout=10)
    r1 = requests.post("https://api.line.me/v2/bot/richmenu",
                       headers={**H, "Content-Type": "application/json"}, json=d["menu"], timeout=15)
    if r1.status_code >= 300:
        return jsonify({"error": f"建選單失敗 {r1.status_code}: {r1.text[:200]}"}), 502
    rid = r1.json()["richMenuId"]
    img = base64.b64decode(d["image_b64"])
    r2 = requests.post(f"https://api-data.line.me/v2/bot/richmenu/{rid}/content",
                       headers={**H, "Content-Type": "image/png"}, data=img, timeout=30)
    if r2.status_code >= 300:
        return jsonify({"error": f"傳圖失敗 {r2.status_code}: {r2.text[:200]}"}), 502
    r3 = requests.post(f"https://api.line.me/v2/bot/user/all/richmenu/{rid}", headers=H, timeout=15)
    if r3.status_code >= 300:
        return jsonify({"error": f"設預設失敗 {r3.status_code}: {r3.text[:200]}"}), 502
    return jsonify({"ok": True, "richMenuId": rid})


def fb_send(psid: str, text: str) -> bool:
    """用 Graph API 把訊息送回去。送完讀回狀態碼——不驗證就又是「印了✅其實沒送」。"""
    try:
        r = requests.post(
            "https://graph.facebook.com/v21.0/me/messages",
            params={"access_token": FB_PAGE_TOKEN},
            json={"recipient": {"id": psid},
                  "messaging_type": "RESPONSE",
                  "message": {"text": text[:2000]}},
            timeout=15)
        if r.status_code != 200:
            print(f"[fb] 送出失敗 {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"[fb] 送出例外：{e}")
        return False


def fb_verify(body: bytes, header_sig: str) -> bool:
    """Meta 的簽章是 sha256=<hex>，算法跟 LINE 不一樣，不能共用 verify_signature。"""
    if not header_sig.startswith("sha256="):
        return False
    mine = hmac.new(FB_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mine, header_sig.split("=", 1)[1])


@app.route("/webhook/messenger", methods=["GET", "POST"])
def webhook_messenger():
    """FB 粉專私訊 —— 即時（Messenger Platform 有 webhook）。

    GET  ＝ Meta 掛 webhook 時的挑戰驗證，要把 hub.challenge 原樣回去
    POST ＝ 真的訊息

    ⚠️ 回覆走 Graph API 的 `me/messages`，不是 LINE 的 replyToken，
    所以不能直接用 `m1_handle`（那支綁死 LINE 的回覆機制）。
    這裡直接叫底層的 `m1.handle()`，邏輯一樣、出口不同。
    """
    if not FB_PAGE_TOKEN or not FB_VERIFY_TOKEN or not FB_APP_SECRET:
        abort(404)

    if request.method == "GET":
        if (request.args.get("hub.mode") == "subscribe"
                and request.args.get("hub.verify_token") == FB_VERIFY_TOKEN):
            return request.args.get("hub.challenge", ""), 200
        return "forbidden", 403

    if not fb_verify(request.get_data(), request.headers.get("X-Hub-Signature-256", "")):
        print("[fb] 簽名驗證失敗")
        abort(400)

    data = request.get_json(silent=True) or {}
    for entry in data.get("entry", []):
        for ev in entry.get("messaging", []):
            if ev.get("message", {}).get("is_echo"):
                continue                      # 自己送的別再處理一次
            psid = (ev.get("sender") or {}).get("id")
            text = (ev.get("message") or {}).get("text")
            if not psid or not text:
                continue
            cfg, active = _inbox_cfg()
            if not active:
                notify_owner(f"⏸ 服務停權中，粉專自動回覆沒送出\n訪客說：{text[:80]}")
                continue
            try:
                r = m1.handle("facebook", psid, text, tid="stanley",
                              cfg=cfg, quiet=True)
            except Exception as e:
                print(f"[fb m1 err] {e}")
                fb_send(psid, "收到你的訊息了，我們會盡快回覆你。")
                notify_owner(f"⚠️ 粉專 M1 出錯：{e}\n訪客說：{text[:80]}")
                continue
            if r.get("reply"):
                ok = fb_send(psid, r["reply"])
                _write_status("line_bot",
                              "粉專已回覆" if ok else "粉專回覆失敗",
                              {"messages_today": _bump_messages_today()})
            if r.get("needs_human"):
                why = r.get("unsure") or f"規則：{r.get('rule') or '沒命中'}"
                notify_owner(f"🔔 粉專要人工接手\n訪客說：{text[:80]}\n（{why}）")
    return "OK"


@app.route("/webhook/ferryman", methods=["POST"])
def webhook_ferryman():
    """擺渡人·Stanley OA（**@907ajfor**）——純對客帳號：所有訊息一律走 M1。

    ⚠️ 2026-08-26 更正：原註解寫 `@078jnspi`，那是**存錢挑戰**那個接案 OA，
    不是這條路綁的帳號。實際綁的是 `@907ajfor`
    （LINE Developers channel 2011221619，webhook 已開）。
    註解寫錯帳號會害人查錯地方，這種錯比沒註解更糟。

    鑰匙沒設就當這條路不存在（404），部署了也不會誤接。
    **404 vs 400 是判斷「鑰匙設了沒」的方法**：
    線上打這條路回 400（簽章錯）＝鑰匙已設；回 404 ＝鑰匙沒設。
    2026-08-26 實測回 400，所以 Render 上的環境變數是齊的。
    """
    if not FERRYMAN_CHANNEL_SECRET or not FERRYMAN_CHANNEL_TOKEN:
        abort(404)
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    if not verify_signature(body.encode(), signature, secret=FERRYMAN_CHANNEL_SECRET):
        print("[ferryman] 簽名驗證失敗")
        abort(400)
    for event in json.loads(body).get("events", []):
        if event["type"] == "follow":
            # 新人前五分鐘的腳本（見 skill: community-architecture / onboarding.md）
            # 三個原則：①分兩則給節奏 ②三個選項但只要挑一個 ③第三個選項＝識別問題
            # 第三題問「哪一行＋最常被問什麼」不是寒暄——答案直接對到 presets/*.json
            # （ecommerce／service／marketing／food／repair），能決定要示範哪一套規則。
            reply_message(event["replyToken"], [
                "嗨，我是擺渡人。\n"
                "我幫店家把 LINE 官方帳號變成會自己接訊息、整理名單的訊息管家。",

                "三件事，挑一件做就好：\n\n"
                "① 想先看它怎麼運作 → 回「示範」\n"
                "② 想知道多少錢 → 回「方案」\n"
                "③ 想讓我看看你的情況 → 直接跟我說你是做哪一行的、\n"
                "　　現在最常被客人問到什麼\n\n"
                "其他事慢慢說，我都會看到。",
            ], token=FERRYMAN_CHANNEL_TOKEN)
        elif event["type"] == "message" and event["message"]["type"] == "text":
            m1_handle(event["source"].get("userId", "unknown"),
                      event["message"]["text"], event["replyToken"],
                      token=FERRYMAN_CHANNEL_TOKEN)
            _write_status("line_bot", "擺渡人OA已回覆", {"messages_today": _bump_messages_today()})
    return "OK"

@app.route("/webhook/t/<tid>", methods=["POST"])
def webhook_tenant(tid):
    """一店一條路。開新客戶＝本機跑一行 `tenant_keys.py --set`，不用進 Render、不用重部署。

    鑰匙從哪裡來由 `line_keys` 那塊決定（env 優先，其次雲端加密欄位）——
    這裡不該知道它存在哪，換存法不必動這支。
    鑰匙沒設就當這條路不存在（404）——部署了也不會誤接別人的訊息。
    ⚠️ tid 直接進 SQL 查詢，所以只收英數與底線，別的一律擋掉。"""
    if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", tid or ""):
        abort(404)
    secret, token = line_keys.get(tid)
    if not secret or not token:
        abort(404)
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    if not verify_signature(body.encode(), signature, secret=secret):
        print(f"[{tid}] 簽名驗證失敗")
        abort(400)
    for event in json.loads(body).get("events", []):
        if event["type"] == "message" and event["message"]["type"] == "text":
            m1_handle(event["source"].get("userId", "unknown"),
                      event["message"]["text"], event["replyToken"],
                      token=token, tid=tid)
            _write_status("line_bot", f"{tid} 已回覆",
                          {"messages_today": _bump_messages_today()})
        elif event["type"] == "message" and event["message"]["type"] == "image":
            # 收據入帳（2026-09-20，「用系統代替文書職缺」第一塊）：老闆或員工把發票／收據拍給店的 OA，
            # 圖存進 Storage、開一筆待處理，本機 clerk_photo.py 讀圖抽欄位寫回，再用店的 token 回報。
            # 誰都能拍（店員也要能用），但只有 tenants.config.clerk.enabled 為 true 的店才收。
            cfg, active = _tenant_cfg(tid)
            if active and (cfg or {}).get("clerk", {}).get("enabled"):
                n = clerk_put(tid, event["message"]["id"], event["source"].get("userId", ""), token)
                reply_message(event["replyToken"],
                              "📎 收據收到了，正在讀（約 1 分鐘）。讀好會回你：店名／日期／金額／類別。" +
                              (f"\n今天第 {n} 張。" if n else ""), token=token)
                _write_status("line_bot", f"{tid} 收據入帳", {"messages_today": _bump_messages_today()})
    return "OK"


def clerk_put(tid: str, mid: str, uid: str, token: str) -> int:
    """把 LINE 圖抓下來放 Storage（bucket clerk，公開讀），開一筆 module_status clerk:<tid>:<mid> pending。
    回今天這家店第幾張（回覆用）。失敗就丟例外讓上面回錯誤，不要安靜。"""
    r = requests.get(f"https://api-data.line.me/v2/bot/message/{mid}/content",
                     headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    url = os.environ.get("SUPABASE_URL", ""); key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    path = f"{tid}/{mid}.jpg"
    up = requests.post(f"{url}/storage/v1/object/clerk/{path}",
                       headers={"apikey": key, "Authorization": f"Bearer {key}",
                                "Content-Type": r.headers.get("Content-Type", "image/jpeg"), "x-upsert": "true"},
                       data=r.content, timeout=60)
    if up.status_code >= 300:
        raise RuntimeError(f"storage {up.status_code}: {up.text[:120]}")
    now = (datetime.utcnow() + timedelta(hours=8)).isoformat(timespec="seconds")
    supa.upsert("module_status", [{"module_name": f"clerk:{tid}:{mid}", "last_run": now, "status": "pending",
                                   "detail": {"tenant": tid, "uid": uid, "img": f"{url}/storage/v1/object/public/clerk/{path}",
                                              "pending": True, "at": now}}], "module_name")
    try:
        today = now[:10]
        rows = supa.select("module_status", f"module_name=like.clerk:{tid}:*&last_run=gte.{today}&select=module_name", tenant="*")
        return len(rows or [])
    except Exception:
        return 0

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
                        reply_owner(reply_token,
                                    f"📮 收到，已放進星空的信箱（未讀 {n} 封）\n"
                                    f"他在電腦前的話會馬上看到；沒開機就等下次開工。")
                    except Exception as e:
                        print(f"[cc_put err] {e}")
                        reply_owner(reply_token, "📮 信箱寫不進去，等等再試一次 😅")
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
                reply_owner(reply_token, alert_reply)
            else:
                # AI 走本機大腦佇列（10-40s），不能卡住 webhook（LINE 會逾時重送）。
                # 先秒回一句「思考中」用 reply，真答案在背景算完用 push 送。
                reply_owner(reply_token, "小星空想一下喔 🌟")
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

        # 非主人傳圖給小星空：stanley 這個租戶開了收據入帳就收（測試用；正式客戶走 webhook_tenant）
        elif (event["type"] == "message" and event["message"]["type"] == "image"
              and event["source"].get("userId") != OWNER_UID
              and ((_inbox_cfg()[0] or {}).get("clerk", {}).get("enabled"))):
            try:
                n = clerk_put("stanley", event["message"]["id"], event["source"].get("userId", ""), FERRYMAN_CHANNEL_TOKEN)
                reply_message(event["replyToken"], "📎 收據收到了，正在讀（約 1 分鐘）。讀好會回你：店名／日期／金額／類別。" + (f"\n今天第 {n} 張。" if n else ""))
            except Exception as e:
                print(f"[clerk_put err] {e}")
                reply_message(event["replyToken"], "📎 收據收到了，但這張存不進去，等等再傳一次 😅")
            _write_status("line_bot", "收據入帳", {"messages_today": _bump_messages_today()})

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
