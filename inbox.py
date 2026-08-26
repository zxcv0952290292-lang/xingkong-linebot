#!/usr/bin/env python3
"""M1 訊息與名單。共用積木第 9 支。

一人公司的第一個水平模組：任何生意都需要「訊息自動接住＋名單自動整理」。
流程六站，這支管前五站：

    接住 → 認人 → 分類 → 回應 → 記錄   （跟進靠排程另外做）

**這支程式不認識任何行業。** 階段名、關鍵字、回覆範本全部來自
租戶設定檔的 inbox 段（行業 preset 打底，見 presets/README.md）。

    import inbox
    r = inbox.handle("line", "U1234", "請問怎麼報名", name="小明")
    # → {"reply": "…", "rule": "challenge", "needs_human": False, ...}

CLI（測試用）：
    python3 inbox.py --dry "請問價格多少"        # 只看分類與回覆，不碰資料庫
    python3 inbox.py --handle "我要報名" --uid test_u1 --name 測試員
    python3 inbox.py --audit                     # 規則體檢：哪幾條是繼承來的、會說出什麼話
    python3 inbox.py --contacts                  # 看名單
    python3 inbox.py --pending                   # 待人工接手的訊息
    TENANT=002 python3 inbox.py --dry "..."      # 換租戶
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import tenant  # 本機才有；雲端（line_bot_deploy）沒有 tenants/，一律走 cfg 參數
except ImportError:
    tenant = None

TZ = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


_OPTIONAL = re.compile(r"\{\?(\w+)\}(.*?)\{/\1\}", re.S)


def _render(tpl: str, v: dict) -> str:
    """把 {brand} {link} {name} 換成值。沒有的變數原樣留著，不炸。

    另外支援選擇性區塊 `{?today}今天狀況：{today}。{/today}`——
    那個變數是空的，整段連同標點一起消失。沒有這個，店主哪天沒填
    「今天的狀況」，客人就會收到「今天的狀況：。」這種半句話。
    """
    tpl = _OPTIONAL.sub(
        lambda m: m.group(2) if str(v.get(m.group(1), "")).strip() else "", tpl or "")
    return re.sub(r"\{(\w+)\}", lambda m: str(v.get(m.group(1), m.group(0))), tpl)


def _cfg(tid: str | None, cfg: "dict | None") -> dict:
    """設定來源：呼叫端直接給（雲端從 tenants.config 讀出來傳進來），
    沒給就讀本機租戶檔。雲端沒有 tenants/ 目錄，一定要用 cfg 參數。"""
    if cfg is not None:
        return cfg
    if tenant is None:
        raise RuntimeError("這個環境沒有 tenants/ 目錄，呼叫時必須帶 cfg 參數")
    return tenant.inbox(tid)


# ── 不確定就閉嘴 ────────────────────────────────────────────
# 講錯話比不講貴：客人得到一句自信的錯答案，不會再問第二次，老闆也永遠不知道。
# 以下三種情形一律**不自動回**，改成叫人。判準刻意只用看得到的證據，不猜語意。
FUTURE_WORDS = ("明天", "明日", "後天", "下週", "下星期", "下禮拜", "禮拜", "星期",
                "週一", "週二", "週三", "週四", "週五", "週六", "週日",
                "假日", "連假", "過年", "初一", "初二", "號那天")
# 回覆裡出現這些字＝那句話講的是「寫死的今天／每天」，客人問別天就會答錯
TIME_FIXED_WORDS = ("今天", "每天", "公休", "營業時間")
UNSURE_REPLY = "訊息收到了，這題我幫你確認一下，馬上回你。"


def _hits(text: str, rules: list) -> list:
    """所有命中的規則（不是只有第一條），連同它命中的**所有**關鍵字。

    原本是「命中第一條就停」，所以「同時像兩件事」的訊息看起來跟
    「明確就是這件事」完全一樣。要判斷有沒有把握，得先知道命中了幾條。
    """
    out = []
    for r in rules:
        kws = [kw for kw in (r.get("keywords") or []) if kw and kw in text]
        if kws:
            out.append((r, kws))
    return out


def _same_thing(groups: list) -> bool:
    """這幾條規則是不是在講同一件事：任兩條之間有互為子字串的關鍵字就算。

    ⚠️ 沒有這一關會誤殺：機車行「換機油多少錢」同時命中 quote（多少錢／換機油）
    與 maintain（機油），但那是關鍵字重疊，不是語意模糊——quote 的答案是對的。
    所以要比對**每條命中的全部關鍵字**，不能只挑一個代表。
    """
    for i, a in enumerate(groups):
        for b in groups[i + 1:]:
            if not any(x in y or y in x for x in a for y in b):
                return False
    return True


def _unsure(text: str, hits: list, reply: str) -> str:
    """要不要閉嘴。回傳原因，空字串＝有把握，照常自動回。"""
    # 閉嘴要擋的是「系統講了錯話而沒有人會發現」。要採用的那條本來就會叫人的話，
    # 老闆一定看得到，而它的話術（「留車型和時間」之類）比一句「我確認一下」有用。
    # 這一關刻意放寬——關太緊會讓系統什麼都不敢答，那比答錯更沒價值。
    if hits[0][0].get("notify"):
        return ""
    if len(hits) > 1 and not _same_thing([kws for _, kws in hits]):
        return "同時命中 " + "／".join(r.get("id", "?") for r, _ in hits[:3])
    kw = max(hits[0][1], key=len)
    if len(kw) < 2 and len(text) > 4:
        return f"只靠單字「{kw}」命中，但客人寫了 {len(text)} 個字"
    if any(w in text for w in FUTURE_WORDS) and any(w in reply for w in TIME_FIXED_WORDS):
        return "客人問的是別天，這條回的卻是寫死的今天／每天"
    return ""


def classify(text: str, tid: str | None = None, cfg: "dict | None" = None) -> dict:
    """分類＋決定回覆。純函式，不碰資料庫——測試和 --dry 都走這裡。"""
    cfg = _cfg(tid, cfg)
    v = dict(cfg.get("vars") or {})
    hits = _hits(text, cfg.get("rules") or [])
    if hits:
        hit = hits[0][0]
        reply = _render(hit.get("reply", ""), v)
        why = _unsure(text, hits, reply)
        if why:
            # 沒把握就不冒充有答案：rule 留空，這則會被算進「沒對到你設的問答」，
            # 月報看得到、規則回顧時就知道要補哪一條。階段也不動。
            return {"rule": "", "reply": UNSURE_REPLY, "needs_human": True,
                    "set_stage": "", "add_tags": [], "unsure": why}
        return {
            "rule": hit.get("id", ""),
            "reply": reply or None,
            "needs_human": bool(hit.get("notify")),
            "set_stage": hit.get("set_stage", ""),
            "add_tags": list(hit.get("add_tags") or []),
            "unsure": "",
        }
    fb = cfg.get("fallback") or {}
    return {
        "rule": "",
        "reply": _render(fb.get("reply", ""), v) or None,
        "needs_human": bool(fb.get("notify", True)),
        "set_stage": "",
        "add_tags": [],
        "unsure": "",
    }


# 示範用的假字。開通給真客人之前一句都不准留。
PLACEHOLDERS = ("【示範", "【範例", "TODO", "XXX", "○○", "待填", "佔位", "範例：")


def _fake_words(text: str) -> list:
    """這句話裡有沒有假字或沒填進去的變數。"""
    bad = [f"示範字「{w}」" for w in PLACEHOLDERS if w in text]
    bad += [f"沒填的變數 {m}" for m in re.findall(r"\{[A-Za-z_]\w*\}", text)]
    return bad


def audit(tid: str | None = None, cfg: "dict | None" = None) -> list:
    """開店關卡：把「這個租戶實際會自動說出口的每一句話」攤開來看。

    繼承來的 preset 規則最危險——它會講出這家店根本沒有的東西
    （例：叫沒有圖文選單的店家客人「看選單的購物說明」）。
    開一家新店，這張表要逐條唸過，繼承的不是覆寫就是 drop 掉。
    """
    cfg = _cfg(tid, cfg)
    own = set(cfg.get("_own_ids") or [])
    v = dict(cfg.get("vars") or {})
    out = []
    for r in cfg.get("rules") or []:
        rid = r.get("id", "")
        out.append({
            "id": rid,
            "source": "租戶" if rid in own else "繼承",
            "keywords": list(r.get("keywords") or []),
            "reply": _render(r.get("reply", ""), v),
            "notify": bool(r.get("notify")),
        })
    seen: dict = {}
    for row in out:
        warn = []
        for kw in row["keywords"]:
            if len(kw) < 2:
                warn.append(f"「{kw}」太短，會亂命中")
            elif kw in seen:
                warn.append(f"「{kw}」已被上面的 {seen[kw]} 先吃掉")
            else:
                seen[kw] = row["id"]
        row["warn"] = warn
    fb = cfg.get("fallback") or {}
    out.append({
        "id": "(沒命中時)",
        "warn": [],
        "source": "租戶" if (cfg.get("_fallback_own")) else "繼承",
        "keywords": [],
        "reply": _render(fb.get("reply", ""), v),
        "notify": bool(fb.get("notify", True)),
    })
    for row in out:
        row["fake"] = _fake_words(row["reply"])
    return out


def _first_stage(cfg: dict) -> str:
    p = cfg.get("pipeline") or []
    return p[0].get("key", "") if p else ""


def _upsert_contact(channel: str, uid: str, name: str, r: dict,
                    tid: str | None, cfg: dict) -> "int | None":
    """認人。讀→改→寫，所以讀取一律 select_strict（select 失敗回 [] 會做出錯資料）。"""
    import supa
    q = f"channel=eq.{channel}&channel_uid=eq.{uid}"
    rows = supa.select_strict("contacts", q, tenant=tid)
    if rows:
        c = rows[0]
        patch: dict = {"last_seen": _now()}
        if name and not c.get("display_name"):
            patch["display_name"] = name
        if r["set_stage"]:
            patch["stage"] = r["set_stage"]
        tags = list(c.get("tags") or [])
        new_tags = [t for t in r["add_tags"] if t not in tags]
        if new_tags:
            patch["tags"] = tags + new_tags
        supa.update("contacts", f"id=eq.{c['id']}", patch, tenant=tid)
        return c["id"]
    supa.insert("contacts", [{
        "channel": channel,
        "channel_uid": uid,
        "display_name": name or "",
        "stage": r["set_stage"] or _first_stage(cfg),
        "tags": r["add_tags"],
        "first_seen": _now(),
        "last_seen": _now(),
    }], tenant=tid)
    rows = supa.select_strict("contacts", q, tenant=tid)
    return rows[0]["id"] if rows else None


def handle(channel: str, uid: str, text: str, name: str = "",
           tid: str | None = None, cfg: "dict | None" = None,
           quiet: bool = False) -> dict:
    """接住一則訊息，跑完認人→分類→回應→記錄，回傳該回什麼。

    呼叫端（LINE webhook、之後的 IG）拿 reply 去回；reply 是 None 就不回。
    quiet=False 且 needs_human=True 時發本機通知；雲端呼叫端傳 quiet=True
    自己用 LINE push 通知（雲端沒有 notify.py）。
    """
    import supa
    cfg = _cfg(tid, cfg)
    r = classify(text, tid, cfg)
    cid = _upsert_contact(channel, uid, name, r, tid, cfg)
    supa.insert("conversations", [
        {"contact_id": cid, "direction": "in", "channel": channel,
         "text": text, "matched_rule": r["rule"],
         "needs_human": r["needs_human"], "created_at": _now()},
    ] + ([
        {"contact_id": cid, "direction": "out", "channel": channel,
         "text": r["reply"], "matched_rule": r["rule"],
         "needs_human": False, "created_at": _now()},
    ] if r["reply"] else []), tenant=tid)
    if r["needs_human"] and not quiet:
        try:
            import notify
            who = name or uid
            tname = tenant.resolve(tid) if tenant else (tid or "?")
            why = r.get("unsure") or ""
            reason = f"沒把握（{why}）" if why else f"規則 {r['rule'] or '沒命中'}"
            notify.info("收訊", f"{who}：{text[:60]}",
                        f"租戶 {tname}／{reason}，要人工接手")
        except ImportError:
            pass
    return dict(r, contact_id=cid)


def main() -> int:
    ap = argparse.ArgumentParser(description="M1 訊息與名單")
    ap.add_argument("--dry", metavar="TEXT", help="只分類不落庫")
    ap.add_argument("--handle", metavar="TEXT", help="完整跑一遍（會寫進資料庫）")
    ap.add_argument("--uid", default="test_user", help="測試用的 channel_uid")
    ap.add_argument("--name", default="", help="顯示名稱")
    ap.add_argument("--channel", default="line")
    ap.add_argument("--tenant", default=None, help="租戶（預設看 TENANT 環境變數）")
    ap.add_argument("--audit", action="store_true", help="規則體檢（開店關卡）")
    ap.add_argument("--contacts", action="store_true", help="看名單")
    ap.add_argument("--pending", action="store_true", help="待人工接手的訊息")
    a = ap.parse_args()

    if a.dry:
        print(json.dumps(classify(a.dry, a.tenant), ensure_ascii=False, indent=2))
        return 0

    if a.handle:
        r = handle(a.channel, a.uid, a.handle, name=a.name, tid=a.tenant, quiet=True)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    if a.audit:
        rows = audit(a.tenant)
        inherited = [r for r in rows if r["source"] == "繼承"]
        for r in rows:
            mark = "⚠️ " if r["source"] == "繼承" else "   "
            kw = "、".join(r["keywords"]) or "—"
            print(f"{mark}[{r['source']}] {r['id']:<12} 觸發字：{kw}")
            print(f"        會回：{r['reply'] or '（不回，只通知）'}"
                  f"{'  ＋叫人' if r['notify'] else ''}")
            for w in r.get("warn") or []:
                print(f"        ⚠️ 觸發字 {w}")
            for w in r.get("fake") or []:
                print(f"        ⛔ 這句會對客人說出{w}")
        bad = sum(len(r.get("warn") or []) for r in rows)
        fake = sum(len(r.get("fake") or []) for r in rows)
        print(f"\n共 {len(rows)} 條，其中 {len(inherited)} 條是行業 preset 繼承來的，"
              f"觸發字問題 {bad} 個，假字 {fake} 句。")
        if fake:
            print("⛔ 有假字就還不能開通——把 vars 換成這家店真的內容再跑一次。")
        if inherited:
            print("繼承的每一條都要唸過：講的是這家店真的有的東西嗎？"
                  "不是就在租戶檔用同 id 覆寫，或加進 drop。")
        return 0

    if a.contacts:
        import supa
        for c in supa.select("contacts", "order=last_seen.desc&limit=30", tenant=a.tenant):
            tags = "、".join(c.get("tags") or [])
            print(f"{c['id']:>4}  {c.get('display_name') or c['channel_uid']:<16} "
                  f"[{c.get('stage','')}]  {tags}")
        return 0

    if a.pending:
        import supa
        for m in supa.select("conversations",
                             "needs_human=eq.true&direction=eq.in&order=created_at.desc&limit=30",
                             tenant=a.tenant):
            print(f"{m['created_at']}  #{m.get('contact_id')}  {m['text'][:60]}")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
