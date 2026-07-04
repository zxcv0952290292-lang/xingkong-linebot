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

# ══════════════════════════════════════════════════════════════
#  自選股即時分析 API
#  （原本在 Netlify function，但每日 zip 部署不會 bundle functions →
#   /api/analyze 一直 404。移到這個 Render 持久服務就不會被洗掉。）
# ══════════════════════════════════════════════════════════════
STOCK_UA = "Mozilla/5.0"

def _get_price(code):
    for ex in ("tse", "otc"):
        try:
            r = requests.get(
                "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                params={"ex_ch": f"{ex}_{code}.tw", "json": "1", "delay": "0"},
                timeout=8)
            d = (r.json().get("msgArray") or [None])[0]
            if d and d.get("z") and d["z"] != "-":
                prev = d.get("y")
                return {
                    "name": d.get("n", code),
                    "price": float(d["z"]),
                    "prev": float(prev) if prev not in (None, "-", "") else 0,
                    "exchange": "上市" if ex == "tse" else "上櫃",
                }
        except Exception:
            pass
    return None

def _get_kline(code, exchange):
    suffix = ".TW" if exchange == "上市" else ".TWO"
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{code}{suffix}",
            params={"interval": "1d", "range": "3mo"},
            headers={"User-Agent": STOCK_UA}, timeout=10)
        result = ((r.json().get("chart") or {}).get("result") or [None])[0]
        if not result:
            return None
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
        }
    except Exception:
        return None

def _recent_trading_dates(n=7):
    out, d = [], datetime.now()
    while len(out) < n:
        if d.weekday() < 5:  # 週一~週五
            out.append({"yyyymmdd": d.strftime("%Y%m%d"),
                        "twDate": f"{d.year - 1911}/{d.strftime('%m')}/{d.strftime('%d')}"})
        d -= timedelta(days=1)
    return out

def _get_chip_tse(code):
    for dt in _recent_trading_dates(7):
        try:
            r = requests.get("https://www.twse.com.tw/fund/T86",
                params={"response": "json", "date": dt["yyyymmdd"], "selectType": "ALLBUT0999"},
                timeout=10)
            for row in (r.json().get("data") or []):
                if row[0].strip() == code:
                    iv = lambda x: int(str(x).replace(",", ""))
                    return {"foreign": round(iv(row[4]) / 1000), "trust": round(iv(row[10]) / 1000),
                            "dealer": round(iv(row[11]) / 1000), "total": round(iv(row[18]) / 1000),
                            "date": dt["yyyymmdd"]}
        except Exception:
            pass
    return None

def _get_fundamental_tse(code):
    for dt in _recent_trading_dates(7):
        try:
            r = requests.get("https://www.twse.com.tw/exchangeReport/BWIBBU_d",
                params={"response": "json", "date": dt["yyyymmdd"], "selectType": "ALL"}, timeout=10)
            for row in (r.json().get("data") or []):
                if row[0].strip() == code:
                    return {"pe": row[5], "pb": row[6], "yield": row[3], "date": dt["yyyymmdd"]}
        except Exception:
            pass
    return None

def _get_fundamental_otc(code):
    for dt in _recent_trading_dates(7):
        try:
            r = requests.get("https://www.tpex.org.tw/web/stock/aftertrading/peratio_analysis/pera_result.php",
                params={"l": "zh-tw", "d": dt["twDate"], "stkno": code, "_": "1"},
                headers={"User-Agent": STOCK_UA}, timeout=10)
            j = r.json()
            rows = ((j.get("tables") or [{}])[0].get("data")) or j.get("aaData") or []
            for row in rows:
                if str(row[0]).strip() == code:
                    return {"pe": row[2], "pb": row[6], "yield": row[5], "date": dt["yyyymmdd"]}
        except Exception:
            pass
    return None

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
    info = _get_price(code)
    if not info:
        return jsonify({"error": f"找不到 {code}，請確認代號正確"}), 404

    is_listed = info["exchange"] == "上市"
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        f_kl = pool.submit(_get_kline, code, info["exchange"])
        f_chip = pool.submit(_get_chip_tse, code) if is_listed else None
        f_fund = pool.submit(_get_fundamental_tse if is_listed else _get_fundamental_otc, code)
        kl = f_kl.result()
        chip = f_chip.result() if f_chip else None
        fund = f_fund.result()

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
        chip_text = "上市籌碼當日暫無，請稍後再試" if is_listed else "上櫃三大法人資料暫無"
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
            model="claude-sonnet-4-6", max_tokens=1200,
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
