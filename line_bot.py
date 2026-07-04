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
import anthropic
from collections import defaultdict, deque
from datetime import datetime, timedelta
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
SCAN_TOKEN = os.environ.get("SCAN_TOKEN", "")  # 保護 /tasks/scan_alerts
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
FINMIND = "https://api.finmindtrade.com/api/v4/data"

# 台股代號→中文名對照表（打包成靜態檔，Render 離線也查得到）
try:
    with open(os.path.join(BASE, "stock_names.json"), encoding="utf-8") as _f:
        STOCK_NAMES = json.load(_f)
    print(f"[stock_names] 載入 {len(STOCK_NAMES)} 檔中文名")
except Exception as _e:
    STOCK_NAMES = {}
    print(f"[stock_names] 載入失敗: {_e}")

def _finmind(dataset, code, days=12):
    """FinMind 開放 API（全球可存取，含台灣以外機房），失敗回空 list。"""
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        r = requests.get(FINMIND, params={"dataset": dataset, "data_id": code, "start_date": start}, timeout=12)
        if r.status_code == 200:
            return r.json().get("data") or []
    except Exception:
        pass
    return []

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

def _get_chip(code):
    """三大法人買賣超（FinMind，全球可用，上市櫃通用）。回傳單位：張。"""
    data = _finmind("TaiwanStockInstitutionalInvestorsBuySell", code, 14)
    if not data:
        return None
    last_date = max(r["date"] for r in data)
    agg = {"foreign": 0.0, "trust": 0.0, "dealer": 0.0}
    for r in data:
        if r["date"] != last_date:
            continue
        net = (r.get("buy", 0) - r.get("sell", 0)) / 1000  # 股 → 張
        n = r.get("name", "")
        if "Foreign" in n:
            agg["foreign"] += net
        elif "Trust" in n:
            agg["trust"] += net
        elif "Dealer" in n:
            agg["dealer"] += net
    total = agg["foreign"] + agg["trust"] + agg["dealer"]
    return {"foreign": round(agg["foreign"]), "trust": round(agg["trust"]),
            "dealer": round(agg["dealer"]), "total": round(total),
            "date": last_date.replace("-", "")}

def _get_fundamental(code):
    """本益比/股價淨值比/殖利率（FinMind TaiwanStockPER，全球可用，上市櫃通用）。"""
    data = _finmind("TaiwanStockPER", code, 14)
    if not data:
        return None
    last = data[-1]
    return {"pe": last.get("PER"), "pb": last.get("PBR"),
            "yield": last.get("dividend_yield"), "date": (last.get("date") or "").replace("-", "")}

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
    # 報價+K線、籌碼、基本面 三路並行（大幅縮短等待）
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        f_q = pool.submit(_yahoo_quote, code)
        f_chip = pool.submit(_get_chip, code)
        f_fund = pool.submit(_get_fundamental, code)
        info, kl = f_q.result()
        chip = f_chip.result()
        fund = f_fund.result()

    if not info:
        return jsonify({"error": f"找不到 {code}，請確認代號正確"}), 404
    # 中文名（打包對照表）
    info["name"] = STOCK_NAMES.get(code, code)

    prev = info.get("prev") or 0
    chg_pct = round((info["price"] - prev) / prev * 100, 2) if prev else 0
    chg_str = f"+{chg_pct:.2f}%" if chg_pct >= 0 else f"{chg_pct:.2f}%"

    k_text = (f"MA5:{kl['ma5']} MA10:{kl['ma10']} MA20:{kl['ma20']} RSI:{kl['rsi']} MACD:{kl['macd']} "
              f"布林:{kl['bollDown']}~{kl['bollUp']} 支撐:{kl['support']} 壓力:{kl['resistance']} "
              f"量比:{kl['volRatio']}x 成交量:{kl['latestVol']}張") if kl else "K線資料不足"
    if chip:
        sgn = lambda x: f"+{x}" if x > 0 else f"{x}"
        chip_text = (f"外資:{sgn(chip['foreign'])}張 投信:{sgn(chip['trust'])}張 自營:{sgn(chip['dealer'])}張 "
                     f"三大法人合計:{sgn(chip['total'])}張（{chip['date']}）")
    else:
        chip_text = "三大法人資料暫無"
    fund_text = (f"本益比:{fund['pe']} 股價淨值比:{fund['pb']} 殖利率:{fund['yield']}%（{fund['date']}）"
                 if fund else "基本面資料暫無")

    prompt = f"""你是台股波段分析師。以下是 {code} {info['name']}（{info['exchange']}）完整資料：

現價:{info['price']} 漲跌:{chg_str}
【技術面】{k_text}
【籌碼面】{chip_text}
【基本面】{fund_text}

請給出完整波段分析，輸出純JSON不加代碼框。

【進場價計算規則，必須嚴格遵守】
- entry 絕對不能等於或高於現價，必須是「值得等待的買進價位」
- RSI > 70（過熱）：entry = MA20 附近或支撐價，至少比現價低 5% 以上
- RSI 60~70（偏熱）：entry = MA5 或 MA10 附近，比現價低 2~4%
- RSI 40~60（中性）：entry = MA5 附近或略低於現價 1~2%
- RSI < 40（偏弱）：entry = 支撐價附近，或 null（不適合進場）
- stop_loss 必須低於 entry，設在最近支撐下方 1~2%
- take_profit 基於波段目標，至少比 entry 高 8% 以上，最多參考壓力位

{{
  "story": "2~3句說明這檔近期發生什麼事、為何值得或不值得關注",
  "technical": "均線排列、RSI位置、MACD方向，說明現在處於哪個階段",
  "chip": "外資投信動向、籌碼是否集中，主力態度如何",
  "fundamental": "本益比合不合理、殖利率有無吸引力",
  "suggestion": "進場/觀望/避開，附上一句理由",
  "entry": 數字或null,
  "take_profit": 數字或null,
  "stop_loss": 數字或null,
  "weeks": "預期持有週數如2~3",
  "risk": "低/中/高",
  "risk_note": "最主要的一個風險點",
  "potential": 1到10整數
}}"""

    analysis = {}
    try:
        resp = claude_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=1200,
            messages=[{"role": "user", "content": prompt}])
        text = resp.content[0].text
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            analysis = json.loads(m.group(0))
    except Exception as e:
        print(f"[api_analyze AI error] {e}")

    return jsonify({
        "code": code, "name": info["name"], "exchange": info["exchange"],
        "price": info["price"], "change_pct": chg_pct,
        "kline": kl, "chip": chip, "fundamental": fund,
        **analysis,  # story/technical/chip/fundamental(文字)/suggestion/entry... 覆蓋原始物件
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
            # 先看是不是到價提醒指令，是就直接回、不走 AI
            try:
                alert_reply = handle_alert_command(user_id, user_message) if supa.enabled() else None
            except Exception as e:
                print(f"[alert err] {e}")
                alert_reply = None
            if alert_reply is not None:
                reply_message(reply_token, alert_reply)
            else:
                ai_response = ask_ai(user_id, user_message)
                print(f"[AI回覆]: {ai_response[:80]}")
                reply_message(reply_token, ai_response)
            _write_status("line_bot", "已回覆訊息", {"messages_today": _bump_messages_today()})

    return "OK"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print(f"小星空啟動中... port={port}")
    app.run(host="0.0.0.0", port=port)
