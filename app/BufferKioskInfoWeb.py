# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pymongo
from flask import Flask, request, render_template

from DBConnect import get_db_conn, get_mongo_client, list_envs


TEMPLATE_NAME = "BufferKioskInfo.html"

# 顯示 UpdateTime 用（預設 UTC+8）
DISPLAY_TZ_OFFSET_HOURS = int(os.getenv("BUFFER_INFO_TZ_OFFSET_HOURS", os.getenv("INPUT_TZ_OFFSET_HOURS", "8")))

# 正式機 DB：避免流量，預設不自動查；且做短快取
CACHE_TTL_SEC = int(os.getenv("BUFFER_INFO_CACHE_TTL_SEC", "60"))
_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}

_CLIENTS: dict[str, pymongo.MongoClient] = {}


def _cache_get(key: tuple[Any, ...]) -> Optional[dict[str, Any]]:
    now = time.time()
    # purge
    expired = [k for k, (exp, _) in _CACHE.items() if exp <= now]
    for k in expired:
        _CACHE.pop(k, None)
    hit = _CACHE.get(key)
    if not hit:
        return None
    exp, payload = hit
    if exp <= now:
        _CACHE.pop(key, None)
        return None
    return payload


def _cache_set(key: tuple[Any, ...], payload: dict[str, Any]) -> None:
    _CACHE[key] = (time.time() + CACHE_TTL_SEC, payload)


def _get_client(uri: str) -> pymongo.MongoClient:
    if not uri:
        raise RuntimeError("Mongo URI 未設定（請填入 FISHDB_MONGO_URI / GAMEDB_MONGO_URI）。")
    cached = _CLIENTS.get(uri)
    if cached is not None:
        return cached
    client = get_mongo_client(uri, appname="my_web_etc_buffer_kiosk_info")
    try:
        # ping early for clear error
        client.admin.command("ping")
    except Exception as e:
        # 讓錯誤訊息更可操作（常見：IP 未放行、公司網路擋出站、URI 參數不完整）
        hint = (
            "Mongo 連線失敗（常見原因）：\n"
            "- Atlas Network Access：確認你目前外網 IP 已加入允許清單（或暫時 0.0.0.0/0 測試）\n"
            "- 公司網路/防火牆：是否擋 MongoDB TLS 出站（srv 會解析到 27017/27016 等埠）\n"
            "- URI：確認 user/pwd、authSource、authMechanism、replicaSet（若有）是否正確\n"
            "- 本機：確認已安裝 dnspython（mongodb+srv 需要）\n"
            f"detail: {e}"
        )
        raise RuntimeError(hint) from e
    _CLIENTS[uri] = client
    return client


def _safe_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return int(v)
        s = str(v).strip()
        if s == "":
            return None
        return int(s)
    except Exception:
        return None


def _safe_str(v: Any) -> str:
    return ("" if v is None else str(v)).strip()


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip().replace(",", "")
        if s == "":
            return None
        return float(s)
    except Exception:
        return None


def _normalize_ctrl_levels(ctrl_level: Any) -> list[dict[str, Any]]:
    """
    將 CtrlLevel 正規化成固定最多三階（按 BufferGateValue 由小到大排序）。
    每階輸出：
      - idx: 1..N
      - buffer_gate_value: number|None
      - no_win_gate: number|None
      - exclude_name: list[str]|None
    """
    if not isinstance(ctrl_level, list):
        return []

    parsed: list[dict[str, Any]] = []
    for lv in ctrl_level:
        if not isinstance(lv, dict):
            continue
        gate = _safe_float(lv.get("BufferGateValue"))
        nowin = _safe_float(lv.get("NoWinGate"))
        exc = lv.get("ExcludeName")
        exc_list = None
        if isinstance(exc, list):
            exc_list = [str(x) for x in exc if str(x).strip()]
        parsed.append({"buffer_gate_value": gate, "no_win_gate": nowin, "exclude_name": exc_list})

    gates_only = [x for x in parsed if x.get("buffer_gate_value") is not None]
    gates_only.sort(key=lambda x: float(x["buffer_gate_value"]))  # type: ignore[arg-type]

    out: list[dict[str, Any]] = []
    for i, x in enumerate(gates_only[:3], start=1):
        out.append({"idx": i, **x})
    return out


def _format_update_time(ts: Any) -> str:
    """
    UpdateTime 可能是秒/毫秒/微秒（整數）。無法判斷時原樣回傳字串。
    """
    if ts is None:
        return ""
    try:
        n = int(ts)
    except Exception:
        return str(ts)

    if n > 10**15:  # unlikely
        sec = n / 1_000_000.0
    elif n > 10**12:
        sec = n / 1000.0
    elif n > 10**11:
        # ambiguous; treat as ms if huge
        sec = n / 1000.0
    else:
        sec = float(n)

    try:
        dt_utc = datetime.fromtimestamp(sec, tz=timezone.utc)
        dt_local = dt_utc.astimezone(timezone(timedelta(hours=DISPLAY_TZ_OFFSET_HOURS)))
        return dt_local.strftime("%Y-%m-%d %H:%M:%S") + f" (UTC{DISPLAY_TZ_OFFSET_HOURS:+d})"
    except Exception:
        return str(ts)


def _compute_hit_stage(ctrl_norm: list[dict[str, Any]], buffer_value: Any) -> dict[str, Any]:
    """
    以「帶正負號」的 BufferValue 與 BufferGateValue（由小到大）比對（虧損方向）：

    規則（依你描述）：門檻寫 250000，實際要 -250000 才會觸發
      - 以 signed 比較：bv <= -gv 視為觸發該階

    - active_idx: 0 表示未達第1階閥值；1..N 表示命中第 N 階（觸發多階時取最高階）
    """
    bv = _safe_float(buffer_value)
    if bv is None:
        return {
            "abs": None,
            "active_idx": None,
            "label": "BufferValue 無法解析",
            "detail": "",
        }

    bv_f = float(bv)
    abs_v = abs(bv_f)
    gates = [x for x in ctrl_norm if x.get("buffer_gate_value") is not None]
    if not gates:
        return {"abs": abs_v, "active_idx": None, "label": "無砍牌閥值（CtrlLevel）", "detail": ""}

    active_idx = 0
    for g in gates:
        gv = float(g["buffer_gate_value"])  # type: ignore[arg-type]
        # 門檻 gv 視為「虧損幅度」：觸發條件為 buffer 足夠負向（<= -gv）
        if gv == 0:
            continue
        if gv > 0 and bv_f <= -gv:
            active_idx = int(g["idx"])
        elif gv < 0 and bv_f <= gv:
            # 相容：若資料把門檻存成負數，則直接用 <= gv
            active_idx = int(g["idx"])

    if active_idx == 0:
        g1 = float(gates[0]["buffer_gate_value"])  # type: ignore[arg-type]
        need = (-g1) if g1 > 0 else g1
        detail = f"Buffer={bv_f:g} 尚未觸發第1階（需 <= {need:g}）"
        return {"abs": abs_v, "active_idx": 0, "label": "未達第1階", "detail": detail}

    g_hit = next(x for x in gates if int(x["idx"]) == active_idx)
    gv_hit = float(g_hit["buffer_gate_value"])  # type: ignore[arg-type]
    need_hit = (-gv_hit) if gv_hit > 0 else gv_hit
    detail = f"Buffer={bv_f:g} 已觸發第{active_idx}階（<= {need_hit:g}）"
    return {"abs": abs_v, "active_idx": active_idx, "label": f"第{active_idx}階", "detail": detail}


def _is_hit_merchant_row(r: dict[str, Any]) -> bool:
    """
    「有進砍牌」：已觸發第 1 階以上（active_idx >= 1）。
    """
    hs = r.get("hit_stage") if isinstance(r, dict) else None
    if not isinstance(hs, dict):
        return False
    ai = hs.get("active_idx")
    if ai is None:
        return False
    try:
        return int(ai) >= 1
    except Exception:
        return False


def _apply_merchant_view_filter(rows: list[dict[str, Any]], show_all_merchants: bool) -> list[dict[str, Any]]:
    """
    - show_all_merchants=False（預設）：只顯示「有進砍牌」商戶
    - show_all_merchants=True：顯示全部商戶（含未砍牌）
    """
    if show_all_merchants:
        return rows
    return [r for r in rows if _is_hit_merchant_row(r)]


def _match_contains(hay: Any, needle: str) -> bool:
    if not needle:
        return True
    s = _safe_str(hay).lower()
    return needle.lower() in s


def _apply_merchant_search_filter(
    rows: list[dict[str, Any]],
    api_name: str,
    merchant_name: str,
    merchant_id: str,
    currency: str,
) -> list[dict[str, Any]]:
    api_name = (api_name or "").strip()
    merchant_name = (merchant_name or "").strip()
    merchant_id = (merchant_id or "").strip()
    currency = (currency or "").strip()

    if not (api_name or merchant_name or merchant_id or currency):
        return rows

    # merchant_id: allow number / substring
    merchant_id_i = _safe_int(merchant_id)

    out: list[dict[str, Any]] = []
    for r in rows:
        if api_name and (not _match_contains(r.get("api_name"), api_name)):
            continue
        if merchant_name and (not _match_contains(r.get("merchant_name"), merchant_name)):
            continue
        if currency and (not _match_contains(r.get("currency"), currency)):
            continue
        if merchant_id:
            mid = r.get("merchant_id")
            if merchant_id_i is not None and mid is not None:
                try:
                    if int(mid) != int(merchant_id_i):
                        continue
                except Exception:
                    # fall back to string contains
                    if not _match_contains(r.get("merchant_raw"), merchant_id):
                        continue
            else:
                # fall back to string contains (merchant_raw keeps original)
                if not (_match_contains(r.get("merchant_raw"), merchant_id) or _match_contains(r.get("merchant_id"), merchant_id)):
                    continue
        out.append(r)
    return out


def _handle_buffer_kiosk_info():
    db_env = (request.args.get("db_env", "macross-test") or "macross-test").strip()
    cfg = get_db_conn("BufferKioskInfoWeb", db_env, default_env="macross-test")
    fishdb_uri = str(cfg.get("fishdb_mongo_uri", "")).strip()
    fishdb_db = str(cfg.get("fishdb_db_name", "Buffer")).strip()
    gamedb_uri = str(cfg.get("gamedb_mongo_uri", "")).strip()
    gamedb_db = str(cfg.get("gamedb_db_name", "MainGame")).strip()

    selected_theme = _safe_str(request.args.get("theme", ""))
    include_disabled = (request.args.get("include_disabled", "0") or "0").strip() in ("1", "true", "True", "yes", "Y")
    show_all_merchants = (request.args.get("show_all", "0") or "0").strip() in ("1", "true", "True", "yes", "Y")
    q_api_name = _safe_str(request.args.get("api_name", ""))
    q_merchant_name = _safe_str(request.args.get("merchant_name", ""))
    q_merchant_id = _safe_str(request.args.get("merchant_id", ""))
    q_currency = _safe_str(request.args.get("currency", ""))
    refresh = (request.args.get("refresh", "0") or "0").strip() in ("1", "true", "True", "yes", "Y")

    warning = ""
    meta = ""

    theme_options: list[str] = []
    setting_rows: list[dict[str, Any]] = []
    merchant_rows_all: list[dict[str, Any]] = []

    if not fishdb_uri:
        warning = "尚未設定 fishdb 連線（FISHDB_MONGO_URI）。"
        return render_template(
            TEMPLATE_NAME,
            brand_title="Kiosk Buffer 資訊",
            brand_url="/KioskBufferInfo",
            active_route="kiosk_buffer_info",
            show_env_select=True,
            db_env=db_env,
            db_presets=[e for e in list_envs("BufferKioskInfoWeb") if e != "default"],
            warning=warning,
            meta="",
            theme_options=[],
            selected_theme=selected_theme,
            include_disabled=include_disabled,
            show_all_merchants=show_all_merchants,
            q_api_name=q_api_name,
            q_merchant_name=q_merchant_name,
            q_merchant_id=q_merchant_id,
            q_currency=q_currency,
            setting_rows=[],
            merchant_rows=[],
            cache_status="SKIP",
        )

    # 先撈 Theme 清單（輕量）
    try:
        fish_db = _get_client(fishdb_uri)[fishdb_db]
        setting_col = fish_db.get_collection("KioskBufferSetting")

        theme_filter = {} if include_disabled else {"Enable": True}
        themes = setting_col.distinct("Theme", theme_filter)
        theme_options = sorted([_safe_str(t) for t in themes if _safe_str(t)], key=str.lower)
    except Exception as e:
        print("=== BufferKioskInfoWeb theme list error ===")
        print(traceback.format_exc())
        warning = f"Theme 清單取得失敗：{type(e).__name__}: {e}"
        theme_options = []

    # 若尚未選 Theme，就只顯示清單，不做後續查詢
    if not selected_theme:
        return render_template(
            TEMPLATE_NAME,
            brand_title="Kiosk Buffer 資訊",
            brand_url="/KioskBufferInfo",
            active_route="kiosk_buffer_info",
            show_env_select=True,
            db_env=db_env,
            db_presets=[e for e in list_envs("BufferKioskInfoWeb") if e != "default"],
            warning=warning,
            meta="請先選擇 Theme。",
            theme_options=theme_options,
            selected_theme="",
            include_disabled=include_disabled,
            show_all_merchants=show_all_merchants,
            q_api_name=q_api_name,
            q_merchant_name=q_merchant_name,
            q_merchant_id=q_merchant_id,
            q_currency=q_currency,
            setting_rows=[],
            merchant_rows=[],
            cache_status="SKIP",
        )

    cache_key = ("theme", selected_theme, include_disabled)
    if not refresh:
        cached = _cache_get(cache_key)
        if cached:
            rows_all = cached.get("merchant_rows_all")
            if rows_all is None:
                # 相容舊快取
                rows_all = cached.get("merchant_rows", []) or []
            base_view = _apply_merchant_view_filter(list(rows_all), show_all_merchants)
            merchant_view = _apply_merchant_search_filter(
                base_view,
                api_name=q_api_name,
                merchant_name=q_merchant_name,
                merchant_id=q_merchant_id,
                currency=q_currency,
            )
            cached_meta = (cached.get("meta") or "") + f" | view_rows={len(merchant_view)}"
            return render_template(
                TEMPLATE_NAME,
                brand_title="Kiosk Buffer 資訊",
                brand_url="/KioskBufferInfo",
                active_route="kiosk_buffer_info",
                show_env_select=True,
                db_env=db_env,
                db_presets=[e for e in list_envs("BufferKioskInfoWeb") if e != "default"],
                warning=(cached.get("warning") or warning),
                meta=cached_meta + " | cache=HIT",
                theme_options=theme_options,
                selected_theme=selected_theme,
                include_disabled=include_disabled,
                show_all_merchants=show_all_merchants,
                q_api_name=q_api_name,
                q_merchant_name=q_merchant_name,
                q_merchant_id=q_merchant_id,
                q_currency=q_currency,
                setting_rows=cached.get("setting_rows", []),
                merchant_rows=merchant_view,
                cache_status="HIT",
            )

    try:
        fish_db = _get_client(fishdb_uri)[fishdb_db]
        setting_col = fish_db.get_collection("KioskBufferSetting")
        value_col = fish_db.get_collection("KioskBufferValue")

        s_q: dict[str, Any] = {"Theme": selected_theme}
        if not include_disabled:
            s_q["Enable"] = True

        setting_rows = list(
            setting_col.find(
                s_q,
                {
                    "_id": 1,
                    "Merchant": 1,
                    "Theme": 1,
                    "Currency": 1,
                    "LineCode": 1,
                    "BufferRate": 1,
                    "CtrlLevel": 1,
                    "Enable": 1,
                    "MaxWin": 1,
                },
            )
        )
        for s in setting_rows:
            s["ctrl_levels_norm"] = _normalize_ctrl_levels(s.get("CtrlLevel"))

        # value：先抓較新的一批，挑出每個 (Merchant,Currency,LineCode) 最新一筆
        v_q: dict[str, Any] = {"Theme": selected_theme}
        raw_values = list(
            value_col.find(
                v_q,
                {"_id": 1, "Merchant": 1, "Theme": 1, "Currency": 1, "LineCode": 1, "UpdateTime": 1, "Value": 1},
            )
            .sort("UpdateTime", pymongo.DESCENDING)
            .limit(2000)
        )
        latest_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
        for r in raw_values:
            m = _safe_str(r.get("Merchant"))
            c = _safe_str(r.get("Currency"))
            l = _safe_str(r.get("LineCode"))
            k = (m, c, l)
            if k not in latest_by_key:
                latest_by_key[k] = r

        # settings map for join
        setting_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
        setting_by_merchant: dict[str, dict[str, Any]] = {}
        default_settings: list[dict[str, Any]] = []
        for s in setting_rows:
            m = _safe_str(s.get("Merchant"))
            c = _safe_str(s.get("Currency"))
            l = _safe_str(s.get("LineCode"))
            setting_by_key[(m, c, l)] = s
            # fallback：同 merchant 取第一筆
            setting_by_merchant.setdefault(m, s)
            if m.lower() == "default":
                default_settings.append(s)

        # gamedb 查詢：用 value 出現的 merchant_id 批次 $in（避免流量）
        merchant_ids: set[int] = set()
        for (m, _c, _l) in latest_by_key.keys():
            mi = _safe_int(m)
            if mi is not None:
                merchant_ids.add(mi)

        game_map: dict[int, dict[str, Any]] = {}
        if merchant_ids and gamedb_uri:
            game_db = _get_client(gamedb_uri)[gamedb_db]
            kiosk_col = game_db.get_collection("KioskSettingInfo")
            cur = kiosk_col.find(
                {"merchant_id": {"$in": sorted(merchant_ids)}},
                {"_id": 1, "ApiName": 1, "merchant_name": 1, "merchant_id": 1},
            ).sort("_id", pymongo.DESCENDING)
            for doc in cur:
                mid = _safe_int(doc.get("merchant_id"))
                if mid is None:
                    continue
                # 只保留最新一筆
                game_map.setdefault(mid, doc)

        merchant_rows_all = []
        for (m, c, l), v in sorted(latest_by_key.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])):
            s = setting_by_key.get((m, c, l))
            if s is None:
                s = setting_by_merchant.get(m)
            if s is None:
                # 若 setting 找不到該 merchant，改用 default（優先同 Currency/LineCode）
                s = setting_by_key.get(("default", c, l))
            if s is None:
                # 次佳：任意 default merchant 的第一筆（維持舊行為：至少能顯示 default 模板）
                s = setting_by_merchant.get("default")
            if s is None and default_settings:
                s = default_settings[0]
            mid = _safe_int(m)
            g = game_map.get(mid) if mid is not None else None
            ctrl_norm = s.get("ctrl_levels_norm") if isinstance(s, dict) else []
            if not isinstance(ctrl_norm, list):
                ctrl_norm = []
            hit = _compute_hit_stage(ctrl_norm, v.get("Value"))
            merchant_rows_all.append(
                {
                    "merchant_id": mid,
                    "merchant_raw": m,
                    "currency": c,
                    "line_code": l,
                    "update_time": v.get("UpdateTime"),
                    "update_time_text": _format_update_time(v.get("UpdateTime")),
                    "buffer_value": v.get("Value"),
                    "buffer_rate": s.get("BufferRate") if isinstance(s, dict) else None,
                    "max_win": s.get("MaxWin") if isinstance(s, dict) else None,
                    "enable": s.get("Enable") if isinstance(s, dict) else None,
                    "ctrl_levels_norm": ctrl_norm,
                    "hit_stage": hit,
                    "api_name": (g.get("ApiName") if isinstance(g, dict) else None),
                    "merchant_name": (g.get("merchant_name") if isinstance(g, dict) else None),
                }
            )

        def _merchant_row_sort_key(r: dict[str, Any]) -> tuple[int, float, int, str, str, str]:
            bv = _safe_float(r.get("buffer_value"))
            mid = _safe_int(r.get("merchant_id"))
            if bv is None:
                return (1, 0.0, mid if mid is not None else 10**18, str(r.get("merchant_raw") or ""), str(r.get("currency") or ""), str(r.get("line_code") or ""))
            return (0, float(bv), mid if mid is not None else 10**18, str(r.get("merchant_raw") or ""), str(r.get("currency") or ""), str(r.get("line_code") or ""))

        merchant_rows_all.sort(key=_merchant_row_sort_key)

        meta = (
            f"theme={selected_theme} | setting_rows={len(setting_rows)} | value_rows(raw)={len(raw_values)} | "
            f"value_rows(latest)={len(merchant_rows_all)} | "
            f"show_all={1 if show_all_merchants else 0} | gamedb_lookup={len(game_map)}"
        )
    except Exception as e:
        print("=== BufferKioskInfoWeb query error ===")
        print(traceback.format_exc())
        warning = f"查詢失敗：{type(e).__name__}: {e}"

    base_view = _apply_merchant_view_filter(merchant_rows_all, show_all_merchants)
    merchant_rows_view = _apply_merchant_search_filter(
        base_view,
        api_name=q_api_name,
        merchant_name=q_merchant_name,
        merchant_id=q_merchant_id,
        currency=q_currency,
    )
    if meta:
        meta = meta + f" | view_rows={len(merchant_rows_view)}"
    payload = {
        "warning": warning,
        "meta": meta,
        "setting_rows": setting_rows,
        "merchant_rows_all": merchant_rows_all,
    }
    _cache_set(cache_key, payload)

    return render_template(
        TEMPLATE_NAME,
        brand_title="Kiosk Buffer 資訊",
        brand_url="/KioskBufferInfo",
        active_route="kiosk_buffer_info",
        show_env_select=True,
        db_env=db_env,
        db_presets=[e for e in list_envs("BufferKioskInfoWeb") if e != "default"],
        warning=warning,
        meta=meta + (" | cache=BYPASS" if refresh else " | cache=MISS"),
        theme_options=theme_options,
        selected_theme=selected_theme,
        include_disabled=include_disabled,
        show_all_merchants=show_all_merchants,
        q_api_name=q_api_name,
        q_merchant_name=q_merchant_name,
        q_merchant_id=q_merchant_id,
        q_currency=q_currency,
        setting_rows=setting_rows,
        merchant_rows=merchant_rows_view,
        cache_status=("BYPASS" if refresh else "MISS"),
    )


def init_buffer_kiosk_info_routes(flask_app: Flask) -> None:
    """
    Register Kiosk Buffer info route.

    - GET /KioskBufferInfo
    - GET /KioskBufferInfo/
    """

    @flask_app.route("/KioskBufferInfo", methods=["GET"], endpoint="kiosk_buffer_info")
    def kiosk_buffer_info():  # noqa: F811
        return _handle_buffer_kiosk_info()

    @flask_app.route("/KioskBufferInfo/", methods=["GET"], endpoint="kiosk_buffer_info_slash")
    def kiosk_buffer_info_slash():  # noqa: F811
        return _handle_buffer_kiosk_info()

