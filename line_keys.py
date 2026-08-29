#!/usr/bin/env python3
"""LINE 鑰匙存放層 —— 一家店的 Channel secret／token 放哪裡、怎麼拿。

① 地基區。只有一件事：**給我 tid，我給你那家店的 (secret, token)。**
上面誰要用（雲端 webhook、本機開通工具）不歸這裡管。

為什麼要有這塊（2026-08-29）：
    原本鑰匙寫在 Render 環境變數 `LINE_SECRET_<TID>` / `LINE_TOKEN_<TID>`。
    每開一家店就要有人登入 Render 貼兩次、再等一次重新部署。
    `開店SOP.md` 自己寫著「第 3 個客戶前改存資料庫（加密）」——就是這塊。

讀取順序（**環境變數優先**，這是刻意的）：
    1. env `LINE_SECRET_<TID>` / `LINE_TOKEN_<TID>`  → 舊的設定原樣繼續走，不會被改壞
    2. 雲端 `tenants.config.line_keys`（Fernet 加密）  → 新開的店走這條，開通不用碰 Render
    兩邊都沒有 → 回 ("", "")，呼叫端當這條路不存在（404）

主金鑰哪裡來：
    有 `TENANT_KEY_SECRET` 就用它；**沒有就從 `SUPABASE_SERVICE_KEY` 推導**。
    推導這條是為了「開通不用碰 Render」——不然為了省掉每店兩個環境變數，
    卻要先去加第三個環境變數，那等於沒省到。
    威脅模型講白話：能讀到這張表的人本來就握著 service key，加密擋的是
    「資料庫內容外流但 service key 沒外流」那種情況（備份、匯出、唯讀權限設錯）。
    要更嚴格就設 `TENANT_KEY_SECRET`，這支會自動改用它，不用改程式。
"""
from __future__ import annotations

import base64
import hashlib
import os
import time

from cryptography.fernet import Fernet, InvalidToken

import supa

FIELD = "line_keys"          # 住在 tenants.config 底下的哪一格
_TTL = 300                   # 快取幾秒（webhook 每則訊息都會叫，不能每次都打資料庫）
_cache: dict[str, tuple[float, str, str]] = {}


def _master() -> bytes:
    """主金鑰（Fernet 要的是 32 bytes 的 urlsafe base64）。"""
    raw = os.environ.get("TENANT_KEY_SECRET", "").strip()
    if not raw:
        raw = (supa.KEY or "").strip() if hasattr(supa, "KEY") else ""
        if not raw:
            raw = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not raw:
        raise RuntimeError("沒有主金鑰（TENANT_KEY_SECRET 與 SUPABASE_SERVICE_KEY 都是空的）")
    # 固定的 info 字串——換掉它＝所有已存的鑰匙都解不開，所以帶版本號。
    dk = hashlib.sha256(b"xingkong-line-keys-v1|" + raw.encode()).digest()
    return base64.urlsafe_b64encode(dk)


def encrypt(plain: str) -> str:
    return Fernet(_master()).encrypt(plain.encode()).decode()


def decrypt(blob: str) -> str:
    return Fernet(_master()).decrypt(blob.encode()).decode()


def _from_env(tid: str) -> tuple[str, str]:
    k = tid.upper()
    return (os.environ.get(f"LINE_SECRET_{k}", ""),
            os.environ.get(f"LINE_TOKEN_{k}", ""))


def _from_db(tid: str) -> tuple[str, str]:
    """雲端那格。讀不到、解不開都回空——webhook 寧可 404 也不要 500。"""
    try:
        rows = supa.select("tenants", f"id=eq.{tid}&select=config", tenant="*")
    except Exception as e:
        print(f"[line_keys] 讀 {tid} 失敗：{e}")
        return "", ""
    if not rows:
        return "", ""
    box = ((rows[0].get("config") or {}).get(FIELD) or {})
    if not box.get("secret") or not box.get("token"):
        return "", ""
    try:
        return decrypt(box["secret"]), decrypt(box["token"])
    except (InvalidToken, Exception) as e:      # noqa: B014 - 解不開就是解不開
        print(f"[line_keys] {tid} 解密失敗（主金鑰換過？）：{type(e).__name__}")
        return "", ""


def get(tid: str, *, fresh: bool = False) -> tuple[str, str]:
    """那家店的 (secret, token)。拿不到回 ("", "")。"""
    s, t = _from_env(tid)
    if s and t:
        return s, t
    hit = _cache.get(tid)
    if hit and not fresh and time.time() - hit[0] < _TTL:
        return hit[1], hit[2]
    s, t = _from_db(tid)
    if s and t:                       # ⚠️ 只快取成功的。快取「沒有」會讓剛開通的店卡 5 分鐘。
        _cache[tid] = (time.time(), s, t)
    return s, t


def put(tid: str, secret: str, token: str) -> bool:
    """把鑰匙寫進雲端那格（加密）。

    ⚠️ 讀→改→寫一律走 select_strict：select() 讀不到會回 []，
    接著這裡就會把整份 config 覆蓋成只剩 line_keys（同 [[supa-read-modify-write]]）。
    """
    if not secret or not token:
        raise ValueError("secret／token 不能是空的")
    rows = supa.select_strict("tenants", f"id=eq.{tid}&select=config", tenant="*")
    if not rows:
        raise RuntimeError(f"雲端沒有租戶 {tid}（先跑 python3 tenant.py --push {tid}）")
    cfg = rows[0].get("config") or {}
    cfg[FIELD] = {"secret": encrypt(secret), "token": encrypt(token)}
    return bool(supa.update("tenants", f"id=eq.{tid}", {"config": cfg}, tenant="*"))


def drop(tid: str) -> bool:
    """把鑰匙從雲端拿掉（客戶結束合作時用）。"""
    rows = supa.select_strict("tenants", f"id=eq.{tid}&select=config", tenant="*")
    if not rows:
        return False
    cfg = rows[0].get("config") or {}
    cfg.pop(FIELD, None)
    _cache.pop(tid, None)
    return bool(supa.update("tenants", f"id=eq.{tid}", {"config": cfg}, tenant="*"))


def where(tid: str) -> str:
    """這家店的鑰匙現在從哪裡來——診斷用，不要拿去做判斷。"""
    if all(_from_env(tid)):
        return "env"
    return "db" if all(_from_db(tid)) else "無"
