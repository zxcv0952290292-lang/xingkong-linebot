#!/usr/bin/env python3
"""知識層：規則答不出來的時候，照店家的知識卡回答——或明說不知道。

分區：① 地基區。只被叫，不 import 上面任何一區。

為什麼要這一塊（2026-08-26 壓測）：五家店 73 則真客人會打的字，
**48% 對不到任何規則**，而且那些不是長尾，是「可以刷卡嗎」「有素的嗎」
「等多久」「我剛剛留的可以加兩份嗎」這種每天被問的題。
規則層只覆蓋得到 27%，補規則永遠補不完，而且補的人是 Stanley。

三層分工（規則 → 知識 → 人）：
    規則層  關鍵字對到就秒回。快、免費、可控。
    知識層  ← 這一塊。答不出標準答案，但**知道自己在答什麼**。
    叫人層  只有本人能決定的（議價、客訴、改單）。

## 鐵律：不准編

答不出來就回 `None`，讓呼叫端叫人。編一句聽起來合理的話比不回答貴得多——
客人拿到錯資訊不會再問第二次，老闆也永遠不知道系統講過什麼
（見 CLAUDE.md 交付硬條件第四條，那是唯一保留下來的紅線）。

## 這支不綁任何一種模型

`answer()` 的 `ask` 參數是注入進來的「怎麼問模型」函式：
本機傳 `brain.ask`（走本機大腦，不花錢），雲端傳打 API 的那支。
所以這支住在地基區，不 import brain、也不 import 任何 SDK。

    import brain, knowledge
    knowledge.answer("可以刷卡嗎", card, ask=brain.ask)

## 怎麼證明它活著

    python3 knowledge.py --tenant moto4 "可以刷卡嗎"      # 單題試答
    python3 knowledge.py --tenant moto4 --bench            # 拿壓測沒命中的題目跑覆蓋率
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 模型答不出來時要回的暗號。用罕見字串，避免跟正常回覆撞在一起。
UNKNOWN = "NO_ANSWER"

MAX_REPLY = 120          # 回覆上限（字）。LINE 是對話不是簡報，長了沒人看
MAX_QUESTION = 300       # 超過這個長度的訊息不進知識層——多半是貼了一整段，該叫人

PROMPT = """你是一家店的 LINE 客服，正在回覆客人的訊息。

# 這家店的資料
{card}

# 客人問的
{question}

# 規則
1. **只能用上面「這家店的資料」裡寫的東西回答。** 資料裡沒有的，一個字都不准補。
2. 標「待填」的欄位＝還沒問到，一律當作沒寫。
3. 資料裡**完全沒有相關線索** → 只輸出 {unknown} 這個字，不要解釋、不要道歉、不要猜。
4. 資料裡有**部分**線索就講那部分，不要整題放棄。
   例：客人問「明天八點可以嗎」，資料寫「09:00 開」→ 回「我們九點才開唷」，
   至於能不能排得進去是本人要決定的，就說「我再幫你確認」，**不要替本人答應**。
5. 價格、庫存、收不收刷卡這種資料沒寫的，絕對不要猜——寧可不答也不能答錯。
6. 用店家的口氣直接回客人，{limit} 字以內，不要開場白、不要「您好」、不要署名。
7. 不要提到「資料」「系統」「規則」這些字——客人不知道也不該知道你在讀什麼。

現在回覆："""


GROUND_PROMPT = """你在檢查一句客服回覆有沒有超出店家資料的範圍。

# 店家資料
{card}

# 待檢查的回覆
{reply}

# 判準（嚴格）
- 回覆裡的**每一個事實**（時間、地點、價格、能不能做某件事）都要在店家資料裡找得到。
- 標「待填」的欄位＝資料裡沒有。拿它來回答就是超出範圍。
- **任何承諾或保證**（「沒問題」「可以」「幫你準備好」「一定」）如果資料沒寫，就是超出範圍。
- 「我再幫你確認」「這個要問本人」這類**把事情交回給人**的句子是允許的，
  它沒有宣稱任何事實，不算超出範圍。

回覆裡宣稱的事實都在資料裡 → 只輸出 OK
有任何一處是資料裡沒有的 → 只輸出 NO

只准輸出 OK 或 NO，不要解釋。"""


# 拖延語。整句只有這些、又沒講到卡片裡任何東西＝沒有價值，fallback 本來就會做這件事。
STALL_WORDS = ("再確認", "再問", "幫你確認", "等我問", "馬上跟你說", "馬上回你",
               "稍等", "再回你", "問一下", "確認一下", "確認好")


def _borrows_fact(reply: str, card: str, question: str = "") -> bool:
    """這句回覆有沒有真的用到知識卡裡的東西（品項名、地名、時段、數字）。

    判準刻意粗——它只負責分辨「講了具體的事」與「純粹拖延」，
    細的部分交給 grounded()。卡片要用店家講話的方式寫（「早上九點」而不是只寫 09:00），
    不然這關會把對的答案也一起擋掉。
    """
    # 扣掉客人自己講過的字：模型很愛把問句抄回來當答案
    # （「這團什麼時候結我再問一下老闆」——那不是答案，是複誦）。
    for w in _terms(card) - FILLER - _terms(question):
        if w in reply:
            return True
    return any(n in reply for n in set(re.findall(r"\d{2,}", card)))


def _terms(text: str) -> set:
    """卡片裡所有 2〜4 字的中文片語。

    ⚠️ 不能用 `findall(r"[\u4e00-\u9fff]{2,8}")`——正則是貪婪的，
    「早上九點到晚上七點」會被當成**一個**詞，於是回覆裡的「九點」永遠對不上，
    對的答案也被當成拖延擋掉（2026-08-26 實際踩到）。要逐字切 n-gram。
    """
    out = set()
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        for n in (2, 3, 4):
            for i in range(len(run) - n + 1):
                out.add(run[i:i + n])
    return out


# 卡片裡也會出現的通用詞，拿它們當「講了具體的事」的證據太寬鬆
FILLER = {"店名", "營業", "品項", "點餐", "外送", "口氣", "待填", "服務", "價格", "付款",
          "地址", "資料", "客人", "可以", "沒有", "我們", "直接", "一下", "跟我", "說一聲",
          "現場", "時間", "如果", "以下", "開通", "當天", "還沒", "問到", "店主"}


def _is_stall(reply: str, card: str, question: str = "") -> bool:
    """只是拖延，沒講出任何實質內容。"""
    return (len(reply) < 60
            and any(w in reply for w in STALL_WORDS)
            and not _borrows_fact(reply, card, question))


def grounded(reply: str, card: str, ask, question: str = "") -> bool:
    """擬完稿回頭跟知識卡對帳，查完才准發。

    2026-08-26 實測抓到的兩種編造，就是這關要擋的：
      ① 早餐店「不加蛋、醬料分開沒問題唷」——卡片的客製化欄位是「待填」，
         那句是**替店家答應了一件他沒說過的事**。
      ② 團購「這團什麼時候結我再問一下老闆」——沒有任何事實，只是拖延；
         那件事 fallback 本來就會做，多這一句沒有價值。
    只靠 prompt 叫模型「不要編」擋不住——寬到能部分回答，就寬到能亂承諾。
    """
    if not reply or not card:
        return False
    if _is_stall(reply, card, question):
        print(f"[knowledge] 只是拖延、沒講到實質內容，改叫人：{reply[:36]}")
        return False
    try:
        v = ask(GROUND_PROMPT.format(card=card.strip(), reply=reply.strip()))
    except Exception as e:
        print(f"[knowledge] 查證失敗，當作不通過：{str(e)[:60]}")
        return False          # 查不了就不發。放行才是危險的那一邊
    return _clean(v).upper().startswith("OK")


def _clean(s: str) -> str:
    """把模型可能多加的殼剝掉：引號、程式碼框、開場白。"""
    s = (s or "").strip()
    s = re.sub(r"^```[a-z]*\n?|```$", "", s, flags=re.M).strip()
    s = re.sub(r"^[「『\"']|[」』\"']$", "", s).strip()
    s = re.sub(r"^(回覆|答案|回答)[:：]\s*", "", s)
    return s.strip()


def card_of(cfg: "dict | None") -> str:
    """從 M1 設定裡取出這家店的知識卡。

    卡片住在租戶檔的 `inbox.knowledge`（一段純文字），跟著 `tenant.py --push`
    上雲，所以雲端 webhook 拿到的 cfg 裡本來就有，不用另外查一次資料庫。
    """
    return str((cfg or {}).get("knowledge") or "").strip()


def answer(question: str, card: str, ask, *, limit: int = MAX_REPLY) -> "str | None":
    """照知識卡回答客人。答不出來、或出任何錯，一律回 None（讓呼叫端叫人）。

    `ask` 是注入的模型呼叫函式，簽名 `ask(prompt: str) -> str`。
    這支**永遠不拋例外**——它掛在客人的即時回覆路徑上，
    模型抽風不能讓整條 webhook 跟著倒。
    """
    q = (question or "").strip()
    if not card or not q or len(q) > MAX_QUESTION:
        return None
    prompt = PROMPT.format(card=card.strip(), question=q, unknown=UNKNOWN, limit=limit)
    try:
        raw = ask(prompt)
    except Exception as e:            # 模型掛了＝這題沒答案，不是整條路壞掉
        print(f"[knowledge] 問不到：{str(e)[:80]}")
        return None
    out = _clean(raw)
    if not out or UNKNOWN in out.upper():
        return None
    # 模型偶爾會照樣造句把暗號包在句子裡，或答出一整篇。兩種都當作沒答案。
    if len(out) > limit * 2:
        print(f"[knowledge] 答太長（{len(out)} 字），當作沒把握")
        return None
    # 第二關：回頭跟知識卡對帳。多花一次呼叫，換掉「自信地編一句」的風險。
    if not grounded(out, card, ask, question=q):
        print(f"[knowledge] 查證沒過，改叫人：{out[:40]}")
        return None
    return out


# 壓測題庫：2026-08-26 那 73 則裡「規則對不到」的那些，照店別分。
# 這是驗收基準，不是裝飾——改知識卡之後跑一次就知道覆蓋率往哪邊走。
BENCH = {
    "breakfast2": ["蘿蔔糕不要加蛋 醬料分開", "剛剛那份可以改成內用嗎", "我到了 在門口",
                   "有素的嗎", "小孩過敏 有沒有不含蛋的", "可以刷卡嗎", "我要退錢"],
    "eggcake3": ["原味跟巧克力各三份 六點半到", "我朋友幫我拿可以嗎", "下雨天有出攤嗎",
                 "可以先轉帳嗎", "我剛剛留的可以加兩份嗎", "等多久", "你們有開發票嗎"],
    "moto4": ["後煞車有聲音 會不會很嚴重", "勞工紓困那個機車補助你們有配合嗎",
              "零件要等多久", "我明天早上八點可以嗎", "上次修的地方又壞了", "可以刷卡嗎"],
    "guishan33": ["這團什麼時候結", "我上次的還沒拿到", "可以退嗎"],
}


def bench(tid: str, ask) -> dict:
    """拿壓測沒命中的題目跑一遍，回覆蓋率。**答錯比答不出來嚴重，所以逐題印出來給人看。**"""
    import tenant
    cfg = tenant.inbox(tid)
    card = card_of(cfg)
    qs = BENCH.get(tid, [])
    if not card:
        return {"tid": tid, "n": len(qs), "hit": 0, "rows": [], "why": "沒有知識卡"}
    rows = []
    for q in qs:
        a = answer(q, card, ask)
        rows.append((q, a))
    return {"tid": tid, "n": len(qs), "hit": sum(1 for _, a in rows if a), "rows": rows, "why": ""}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="知識層：拿一家店的知識卡試答")
    ap.add_argument("question", nargs="?", help="客人問的話")
    ap.add_argument("--tenant", default=None)
    ap.add_argument("--card", action="store_true", help="只印這家店的知識卡")
    ap.add_argument("--bench", action="store_true", help="拿壓測沒命中的題目跑覆蓋率")
    a = ap.parse_args()

    import tenant
    tid = tenant.resolve(a.tenant)
    cfg = tenant.inbox(tid)
    card = card_of(cfg)

    if a.card:
        print(card or "（這家店還沒有知識卡——租戶檔 inbox.knowledge 是空的）")
        return 0 if card else 1

    import brain
    if a.bench:
        r = bench(tid, brain.ask)
        if r["why"]:
            print(f"❌ {tid}：{r['why']}")
            return 1
        for q, ans in r["rows"]:
            print(f"  {'✅' if ans else '→ 叫人'}　{q}")
            if ans:
                print(f"        {ans}")
        pct = r["hit"] * 100 // r["n"] if r["n"] else 0
        print(f"\n{tid}：{r['hit']}/{r['n']} 答得出來（{pct}%），其餘轉人工。")
        return 0

    if not a.question:
        ap.error("要嘛給一句客人的話，要嘛用 --card / --bench")
    if not card:
        print("❌ 這家店還沒有知識卡")
        return 1
    r = answer(a.question, card, ask=brain.ask)
    print(f"客人：{a.question}")
    print(f"知識層：{r if r else '（答不出來 → 叫人）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
