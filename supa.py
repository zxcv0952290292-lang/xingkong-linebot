#!/usr/bin/env python3
"""Supabase REST 輕量封裝 — 星空各服務共用。

讀取順序：環境變數 SUPABASE_URL / SUPABASE_SERVICE_KEY，
若不存在則往本層 → 上層 → 上上層找 .env。

設計原則：所有寫入失敗只印訊息、不拋例外，
確保主流程（LINE 回覆、股票推播、IG 分析）不會因資料庫問題中斷。
"""
from __future__ import annotations

import os
import json
from pathlib import Path

import requests


# ── 錯誤日誌節流（2026-09-12）──
# 斷網 30 分鐘，brain_daemon 每 3 秒輪詢兩張表，brain.log 灌了 14,829 行一模一樣的 Max retries，
# 而且沒時間戳，事後連「斷線從幾點開始」都算不出來。這裡：同一種錯第 1 次印、之後每 50 次印一次，帶時間。
_ERR_SEEN: dict = {}
def _elog(key: str, msg: str) -> None:
    import datetime as _dt
    n = _ERR_SEEN.get(key, 0) + 1; _ERR_SEEN[key] = n
    if n == 1 or n % 50 == 0:
        print(f"[supa {_dt.datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}" + (f"（同類第 {n} 次）" if n > 1 else ""), flush=True)
def _eok(key: str) -> None:
    """成功一次就把計數歸零，下次再壞會重新印第 1 次（＝恢復點也看得到）。"""
    if _ERR_SEEN.pop(key, None): print(f"[supa {__import__('datetime').datetime.now().strftime('%m-%d %H:%M:%S')}] {key} 恢復", flush=True)

def _load_env():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if url and key:
        return url, key
    here = Path(__file__).resolve().parent
    for d in (here, here.parent, here.parent.parent):
        f = d / ".env"
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if "=" not in s or s.startswith("#"):
                continue
            k, v = s.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "SUPABASE_URL" and not url:
                url = v
            elif k == "SUPABASE_SERVICE_KEY" and not key:
                key = v
        if url and key:
            break
    return url, key


URL, KEY = _load_env()

# ── 租戶隔離 ──────────────────────────────────────────────
# ⚠️ 這裡用的是 SERVICE KEY，它會**繞過資料庫的 RLS 權限規則**。
#    也就是說：後端的隔離不能靠 RLS，必須靠這一層自動加條件。
#    RLS 仍然要開，那是給前端（anon key）用的第二道。
#
# 開關預設關閉，等 migrations/001_tenant_isolation.sql 真的跑過再打開，
# 不然會對還沒有 tenant_id 欄位的表下條件，整批查詢 400。
#    打開方式：.env 加 TENANT_SCOPE=1
TENANT_TABLES = {
    "module_status", "push_history", "stock_picks",
    "line_chat_history", "ig_analyses", "factory_content",
    "job_queue", "contacts", "conversations",
}


def scope_on() -> bool:
    v = os.environ.get("TENANT_SCOPE")
    if v is None:
        here = Path(__file__).resolve().parent
        f = here / ".env"
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("TENANT_SCOPE="):
                    v = line.split("=", 1)[1].strip()
                    break
    return str(v) == "1"


def _tid(tenant: str | None) -> str:
    return tenant or os.environ.get("TENANT") or "stanley"


def _scoped(table: str, tenant: str | None) -> bool:
    """這次操作要不要加租戶條件。tenant='*' 代表跨租戶（管理用），不加。"""
    return scope_on() and table in TENANT_TABLES and tenant != "*"


def _stamp(table: str, rows: list, tenant: str | None) -> list:
    """寫入時自動蓋上 tenant_id。已經自己填了的不覆蓋。"""
    if not _scoped(table, tenant):
        return rows
    t = _tid(tenant)
    return [{**r, "tenant_id": r.get("tenant_id", t)} for r in rows]


def _filter(table: str, query: str, tenant: str | None) -> str:
    """查詢時自動加租戶條件。呼叫端已經自己帶了就不重複加。"""
    if not _scoped(table, tenant) or "tenant_id=" in query:
        return query
    cond = f"tenant_id=eq.{_tid(tenant)}"
    return f"{query}&{cond}" if query else cond


def enabled() -> bool:
    return bool(URL and KEY)


def _headers(prefer: str) -> dict:
    return {
        "apikey": KEY,
        "Authorization": f"Bearer {KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def insert(table: str, rows: list, tenant: str | None = None) -> bool:
    """新增（append）。"""
    if not enabled() or not rows:
        return False
    rows = _stamp(table, rows, tenant)
    try:
        r = requests.post(
            f"{URL}/rest/v1/{table}",
            headers=_headers("return=minimal"),
            data=json.dumps(rows, ensure_ascii=False).encode("utf-8"),
            timeout=10,
        )
        if r.status_code >= 300:
            _elog(f"insert {table}", f"insert {table} 失敗 {r.status_code}: {r.text[:150]}")
            return False
        return True
    except Exception as e:
        _elog(f"insert {table}", f"insert {table} 例外: {e}")
        return False


def upsert(table: str, rows: list, on_conflict: str, tenant: str | None = None) -> bool:
    """有衝突就覆蓋（依 on_conflict 欄位）。"""
    if not enabled() or not rows:
        return False
    rows = _stamp(table, rows, tenant)
    try:
        r = requests.post(
            f"{URL}/rest/v1/{table}?on_conflict={on_conflict}",
            headers=_headers("resolution=merge-duplicates,return=minimal"),
            data=json.dumps(rows, ensure_ascii=False).encode("utf-8"),
            timeout=10,
        )
        if r.status_code >= 300:
            _elog(f"upsert {table}", f"upsert {table} 失敗 {r.status_code}: {r.text[:150]}")
            return False
        return True
    except Exception as e:
        _elog(f"upsert {table}", f"upsert {table} 例外: {e}")
        return False


def update(table: str, query: str, patch: dict, timeout: int = None,
           tenant: str | None = None) -> bool:
    """只改指定欄位（PATCH），其他欄位保持不動。query 例：id=eq.8

    ⚠️ 不要用 upsert 來「改幾個欄位」——upsert 走的是 POST，沒帶到的欄位會被寫成 NULL，
    撞到 NOT NULL 就整批失敗（2026-07-13 回填 push_history.return_pct 時踩過：
    錯誤 23502，push_date 被寫成 null）。改欄位一律用這支。

    `timeout=None` 會照 payload 大小自動估（每 MB 給 20 秒，下限 10 秒）。
    2026-08-17：輪播 daemon 把 7 張 2x PNG（base64 約 18MB）寫回時，固定 10 秒逾時，
    重試 6 次每次都重傳整包，最後放棄；前端就永遠卡在「Mac 渲染中」。
    """
    if not enabled() or not patch:
        return False
    query = _filter(table, query, tenant)
    try:
        body = json.dumps(patch, ensure_ascii=False).encode("utf-8")
        if timeout is None:                      # 沒指定就照 payload 大小估：每 MB 給 20 秒，至少 10 秒
            timeout = max(10, int(len(body) / 1_000_000 * 20))
        r = requests.patch(
            f"{URL}/rest/v1/{table}?{query}",
            headers=_headers("return=minimal"),
            data=body,
            timeout=timeout,
        )
        if r.status_code >= 300:
            _elog(f"update {table}", f"update {table} 失敗 {r.status_code}: {r.text[:150]}")
            return False
        return True
    except Exception as e:
        _elog(f"update {table}", f"update {table} 例外: {e}")
        return False


def delete(table: str, query: str, tenant: str | None = None) -> bool:
    """刪掉符合 query 的列。query 例：module_name=eq.job:tcctest

    ⚠️ **一定要帶 query**——PostgREST 沒有條件就是刪整張表。空字串直接拒絕，
    不要靠呼叫端記得（同 `_filter` 的租戶隔離，安全條件放在共用積木裡才擋得住）。
    ⚠️ 這支只給「本來就不該存在的列」用（測試殘留、重複列）。
    真實資料要下架一律改狀態，不要刪（出事才找得回來，同 `_retired/` 的道理）。
    """
    if not enabled() or not query.strip():
        return False
    query = _filter(table, query, tenant)
    try:
        r = requests.delete(
            f"{URL}/rest/v1/{table}?{query}",
            headers=_headers("return=minimal"),
            timeout=20,
        )
        if r.status_code >= 300:
            _elog(f"delete {table}", f"delete {table} 失敗 {r.status_code}: {r.text[:150]}")
            return False
        return True
    except Exception as e:
        _elog(f"delete {table}", f"delete {table} 例外: {e}")
        return False


def select(table: str, query: str = "", tenant: str | None = None) -> list:
    """查詢，query 為 PostgREST 參數字串（例：user_id=eq.U123&limit=10）。"""
    if not enabled():
        return []
    query = _filter(table, query, tenant)
    try:
        url = f"{URL}/rest/v1/{table}"
        if query:
            url += f"?{query}"
        r = requests.get(
            url,
            headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"},
            timeout=10,
        )
        if r.status_code >= 300:
            _elog(f"select {table}", f"select {table} 失敗 {r.status_code}: {r.text[:150]}")
            return []
        return r.json()
    except Exception as e:
        _elog(f"select {table}", f"select {table} 例外: {e}")
        return []


def select_strict(table: str, query: str = "", timeout: int = 45,
                  tenant: str | None = None) -> list:
    """讀取失敗就丟例外，不會把「讀不到」偽裝成「沒有資料」。

    2026-08-16 事故：用 select() 讀 posted_topics 台帳做「讀→改→寫」，
    讀取 timeout 回傳 []，接著把 14 筆台帳覆蓋成 2 筆。
    **任何 read-modify-write 一律用這支，不要用 select()。**
    select() 的靜默失敗只適合唯讀、失敗可略過的場景（LINE 回覆、推播）。
    """
    if not enabled():
        raise RuntimeError("supabase 未設定（缺 SUPABASE_URL / SUPABASE_SERVICE_KEY）")
    query = _filter(table, query, tenant)
    url = f"{URL}/rest/v1/{table}"
    if query:
        url += f"?{query}"
    r = requests.get(
        url,
        headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()
