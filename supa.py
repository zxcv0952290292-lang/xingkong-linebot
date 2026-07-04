#!/usr/bin/env python3
"""Supabase REST 輕量封裝 — 星空各服務共用。

讀取順序：環境變數 SUPABASE_URL / SUPABASE_SERVICE_KEY，
若不存在則往本層 → 上層 → 上上層找 .env。

設計原則：所有寫入失敗只印訊息、不拋例外，
確保主流程（LINE 回覆、股票推播、IG 分析）不會因資料庫問題中斷。
"""
import os
import json
from pathlib import Path

import requests


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


def enabled() -> bool:
    return bool(URL and KEY)


def _headers(prefer: str) -> dict:
    return {
        "apikey": KEY,
        "Authorization": f"Bearer {KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def insert(table: str, rows: list) -> bool:
    """新增（append）。"""
    if not enabled() or not rows:
        return False
    try:
        r = requests.post(
            f"{URL}/rest/v1/{table}",
            headers=_headers("return=minimal"),
            data=json.dumps(rows, ensure_ascii=False).encode("utf-8"),
            timeout=10,
        )
        if r.status_code >= 300:
            print(f"[supa] insert {table} 失敗 {r.status_code}: {r.text[:150]}")
            return False
        return True
    except Exception as e:
        print(f"[supa] insert {table} 例外: {e}")
        return False


def upsert(table: str, rows: list, on_conflict: str) -> bool:
    """有衝突就覆蓋（依 on_conflict 欄位）。"""
    if not enabled() or not rows:
        return False
    try:
        r = requests.post(
            f"{URL}/rest/v1/{table}?on_conflict={on_conflict}",
            headers=_headers("resolution=merge-duplicates,return=minimal"),
            data=json.dumps(rows, ensure_ascii=False).encode("utf-8"),
            timeout=10,
        )
        if r.status_code >= 300:
            print(f"[supa] upsert {table} 失敗 {r.status_code}: {r.text[:150]}")
            return False
        return True
    except Exception as e:
        print(f"[supa] upsert {table} 例外: {e}")
        return False


def select(table: str, query: str = "") -> list:
    """查詢，query 為 PostgREST 參數字串（例：user_id=eq.U123&limit=10）。"""
    if not enabled():
        return []
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
            print(f"[supa] select {table} 失敗 {r.status_code}: {r.text[:150]}")
            return []
        return r.json()
    except Exception as e:
        print(f"[supa] select {table} 例外: {e}")
        return []
