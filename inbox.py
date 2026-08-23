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


def _render(tpl: str, v: dict) -> str:
    """把 {brand} {link} {name} 換成值。沒有的變數原樣留著，不炸。"""
    return re.sub(r"\{(\w+)\}", lambda m: str(v.get(m.group(1), m.group(0))), tpl or "")


def _cfg(tid: str | None, cfg: "dict | None") -> dict:
    """設定來源：呼叫端直接給（雲端從 tenants.config 讀出來傳進來），
    沒給就讀本機租戶檔。雲端沒有 tenants/ 目錄，一定要用 cfg 參數。"""
    if cfg is not None:
        return cfg
    if tenant is None:
        raise RuntimeError("這個環境沒有 tenants/ 目錄，呼叫時必須帶 cfg 參數")
    return tenant.inbox(tid)


def classify(text: str, tid: str | None = None, cfg: "dict | None" = None) -> dict:
    """分類＋決定回覆。純函式，不碰資料庫——測試和 --dry 都走這裡。"""
    cfg = _cfg(tid, cfg)
    v = dict(cfg.get("vars") or {})
    hit = None
    for r in cfg.get("rules") or []:
        if any(kw and kw in text for kw in (r.get("keywords") or [])):
            hit = r
            break
    if hit:
        return {
            "rule": hit.get("id", ""),
            "reply": _render(hit.get("reply", ""), v) or None,
            "needs_human": bool(hit.get("notify")),
            "set_stage": hit.get("set_stage", ""),
            "add_tags": list(hit.get("add_tags") or []),
        }
    fb = cfg.get("fallback") or {}
    return {
        "rule": "",
        "reply": _render(fb.get("reply", ""), v) or None,
        "needs_human": bool(fb.get("notify", True)),
        "set_stage": "",
        "add_tags": [],
    }


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
            notify.info("收訊", f"{who}：{text[:60]}",
                        f"租戶 {tname}／規則 {r['rule'] or '沒命中'}，要人工接手")
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
