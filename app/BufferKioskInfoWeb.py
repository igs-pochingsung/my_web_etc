# -*- coding: utf-8 -*-
import json
import os
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import pymongo
from flask import Flask, request, render_template, jsonify

from DBConnect import bq_table_fqn, get_bq_config, get_db_conn, is_bq_configured, get_mongo_client, list_envs


TEMPLATE_NAME = "BufferKioskInfo.html"

# 顯示 UpdateTime 用（預設 UTC+8）
DISPLAY_TZ_OFFSET_HOURS = int(os.getenv("BUFFER_INFO_TZ_OFFSET_HOURS", os.getenv("INPUT_TZ_OFFSET_HOURS", "8")))

# 正式機 DB：避免流量，預設不自動查；且做短快取
CACHE_TTL_SEC = int(os.getenv("BUFFER_INFO_CACHE_TTL_SEC", "60"))
_CACHE = {}

_CLIENTS = {}


def _cache_get(key):
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


def _cache_set(key, payload):
    _CACHE[key] = (time.time() + CACHE_TTL_SEC, payload)


def _get_client(uri):
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


def _safe_int(v):
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


def _safe_str(v):
    return ("" if v is None else str(v)).strip()


def _safe_float(v):
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


def _normalize_ctrl_levels(ctrl_level):
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

    parsed = []
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

    out = []
    for i, x in enumerate(gates_only[:3], start=1):
        out.append({"idx": i, **x})
    return out


def _format_update_time(ts):
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


def _compute_hit_stage(ctrl_norm, buffer_value):
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


def _is_hit_merchant_row(r):
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


def _apply_merchant_view_filter(rows, show_all_merchants):
    """
    - show_all_merchants=False（預設）：只顯示「有進砍牌」商戶
    - show_all_merchants=True：顯示全部商戶（含未砍牌）
    """
    if show_all_merchants:
        return rows
    return [r for r in rows if _is_hit_merchant_row(r)]


def _match_contains(hay, needle):
    if not needle:
        return True
    s = _safe_str(hay).lower()
    return needle.lower() in s


def _apply_merchant_search_filter(rows, api_name, merchant_name, merchant_id):
    api_name = (api_name or "").strip()
    merchant_name = (merchant_name or "").strip()
    merchant_id = (merchant_id or "").strip()

    if not (api_name or merchant_name or merchant_id):
        return rows

    # merchant_id: allow number / substring
    merchant_id_i = _safe_int(merchant_id)

    out = []
    for r in rows:
        if api_name and (not _match_contains(r.get("api_name"), api_name)):
            continue
        if merchant_name and (not _match_contains(r.get("merchant_name"), merchant_name)):
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


def _distinct_nonempty_str(rows, key, limit=500):
    """
    從目前結果集中抽出可用於前端 datalist 的候選值（去重、排序、限量）。
    """
    seen = set()
    out = []
    for r in rows:
        v = _safe_str(r.get(key))
        if not v:
            continue
        k = v.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(v)
        if len(out) >= limit:
            break
    out.sort(key=str.lower)
    return out


def _format_twd_equivalent(val: Optional[float]) -> str:
    if val is None:
        return "-"
    try:
        return f"{float(val):,.3f}"
    except Exception:
        return "-"


def _parse_bq_credentials(api_key: str):
    if not api_key:
        return None
    raw = api_key.strip()
    try:
        if os.path.isfile(raw):
            with open(raw, "r", encoding="utf-8") as f:
                info = json.load(f)
        elif raw.startswith("{"):
            info = json.loads(raw)
        else:
            return None
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_info(info)
    except Exception:
        print("=== BufferKioskInfoWeb BQ credentials parse error ===")
        print(traceback.format_exc())
        return None


def _get_bq_client(cfg: dict):
    if not is_bq_configured(cfg):
        return None
    bq = get_bq_config(cfg)
    creds = _parse_bq_credentials(bq["api_key"])
    if creds is None:
        return None
    try:
        from google.cloud import bigquery

        return bigquery.Client(credentials=creds, project=bq["project"])
    except Exception:
        print("=== BufferKioskInfoWeb BQ client error ===")
        print(traceback.format_exc())
        return None


def _load_bq_merchant_map(client, cfg: dict) -> Dict[int, Dict[str, Any]]:
    tbl = bq_table_fqn(cfg, "merchant_table")
    if not tbl:
        return {}
    sql = f"""
        SELECT MerchantID, Ratio, CreditType
        FROM {tbl}
        WHERE MerchantID IS NOT NULL
    """
    out: Dict[int, Dict[str, Any]] = {}
    try:
        for row in client.query(sql).result():
            mid = _safe_int(row.get("MerchantID"))
            if mid is None:
                continue
            ratio = _safe_float(row.get("Ratio"))
            credit_type = _safe_str(row.get("CreditType"))
            if ratio is None or not credit_type:
                continue
            out[mid] = {"ratio": ratio, "credit_type": credit_type}
    except Exception:
        print("=== BufferKioskInfoWeb BQ DimMerchantInfo error ===")
        print(traceback.format_exc())
        return {}
    return out


def _load_bq_exchange_rate_map(client, cfg: dict) -> Dict[str, Dict[str, Any]]:
    tbl = bq_table_fqn(cfg, "exchange_table")
    if not tbl:
        return {}
    sql = f"""
        SELECT FromCurrency, Rate, Year, Month
        FROM (
            SELECT
                FromCurrency,
                Rate,
                Year,
                Month,
                ROW_NUMBER() OVER (
                    PARTITION BY FromCurrency
                    ORDER BY Year DESC, Month DESC
                ) AS rn
            FROM {tbl}
            WHERE SourceType = 'accounting'
              AND ToCurrency = 'TWD'
              AND FromCurrency IS NOT NULL
              AND Rate IS NOT NULL
        )
        WHERE rn = 1
    """
    out: Dict[str, Dict[str, Any]] = {}
    try:
        for row in client.query(sql).result():
            fc = _safe_str(row.get("FromCurrency"))
            rate = _safe_float(row.get("Rate"))
            year = _safe_int(row.get("Year"))
            month = _safe_int(row.get("Month"))
            if not fc or rate is None:
                continue
            out[fc] = {
                "rate": float(rate),
                "year": year,
                "month": month,
            }
    except Exception:
        print("=== BufferKioskInfoWeb BQ DimExchangeRateReport error ===")
        print(traceback.format_exc())
        return {}
    return out


def _load_bq_currency_relation_map(client, cfg: dict) -> Dict[str, Dict[str, Any]]:
    tbl = bq_table_fqn(cfg, "currency_relation_table")
    if not tbl:
        return {}
    sql = f"""
        SELECT Currency, ZoomCurrency, ZoomRatio
        FROM {tbl}
        WHERE Currency IS NOT NULL
    """
    out: Dict[str, Dict[str, Any]] = {}
    try:
        for row in client.query(sql).result():
            currency = _safe_str(row.get("Currency"))
            zoom_currency = _safe_str(row.get("ZoomCurrency"))
            zoom_ratio = _safe_float(row.get("ZoomRatio"))
            if not currency or not zoom_currency or zoom_ratio is None or zoom_ratio <= 0:
                continue
            out[zoom_currency] = {
                "currency": currency,
                "zoom_currency": zoom_currency,
                "zoom_ratio": float(zoom_ratio),
            }
    except Exception:
        print("=== BufferKioskInfoWeb BQ DimCurrencyInfoRelation error ===")
        print(traceback.format_exc())
        return {}
    return out


def _get_bq_lookup_maps(
    cfg: dict,
) -> Tuple[Dict[int, Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """
    回傳 (merchant_id -> {ratio, credit_type}, FromCurrency -> Rate, ZoomCurrency -> relation)。
    失敗或未設定時回傳空 dict（呼叫端顯示「-」）。
    """
    if not is_bq_configured(cfg):
        return {}, {}, {}

    cache_key = "bq_maps:v6:" + "|".join(
        [
            _safe_str(cfg.get("bq_project")),
            _safe_str(cfg.get("bq_dataset")),
            _safe_str(cfg.get("bq_merchant_info_table_name")),
            _safe_str(cfg.get("bq_exchange_rate_table_name")),
            _safe_str(cfg.get("bq_currency_relation_table_name")),
        ]
    )
    hit = _cache_get(cache_key)
    if isinstance(hit, tuple) and len(hit) == 3:
        return hit

    client = _get_bq_client(cfg)
    if client is None:
        return {}, {}, {}

    merchant_map = _load_bq_merchant_map(client, cfg)
    rate_map = _load_bq_exchange_rate_map(client, cfg)
    currency_relation_map = _load_bq_currency_relation_map(client, cfg)
    payload = (merchant_map, rate_map, currency_relation_map)
    _cache_set(cache_key, payload)
    return payload


def _lookup_rate_entry(
    from_currency: str, rate_map: Dict[str, Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    fc = _safe_str(from_currency)
    if not fc or not rate_map:
        return None
    entry = rate_map.get(fc)
    if entry is None:
        for k, v in rate_map.items():
            if k.lower() == fc.lower():
                entry = v
                break
    if not isinstance(entry, dict):
        return None
    return entry


def _lookup_exchange_rate(
    from_currency: str, rate_map: Dict[str, Dict[str, Any]]
) -> Optional[float]:
    entry = _lookup_rate_entry(from_currency, rate_map)
    if not entry:
        return None
    rate = _safe_float(entry.get("rate"))
    return float(rate) if rate is not None else None


def _format_exchange_rate_period(year, month) -> str:
    y = _safe_int(year)
    m = _safe_int(month)
    if y is None or m is None:
        return ""
    return f"{y}-{m:02d}"


def _lookup_currency_relation_entry(
    zoom_currency: str, currency_relation_map: Dict[str, Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    zc = _safe_str(zoom_currency)
    if not zc or not currency_relation_map:
        return None
    entry = currency_relation_map.get(zc)
    if entry is None:
        for k, v in currency_relation_map.items():
            if k.lower() == zc.lower():
                entry = v
                break
    if not isinstance(entry, dict):
        return None
    return entry


def _resolve_currency_info(
    credit_type: str, currency_relation_map: Dict[str, Dict[str, Any]]
) -> Tuple[str, str, float]:
    """
    DimMerchantInfo.CreditType 對應 DimCurrencyInfoRelation.ZoomCurrency。
    回傳 (顯示幣種 Currency, zoom_currency, zoom_ratio)。
    無對應時顯示幣種=CreditType、zoom_ratio=1。
    """
    ct = _safe_str(credit_type)
    if not ct:
        return "", "", 1.0
    entry = _lookup_currency_relation_entry(ct, currency_relation_map)
    if not entry:
        return ct, ct, 1.0
    display_currency = _safe_str(entry.get("currency")) or ct
    zoom_currency = _safe_str(entry.get("zoom_currency")) or ct
    zoom_ratio = _safe_float(entry.get("zoom_ratio"))
    if zoom_ratio is None or zoom_ratio <= 0:
        zoom_ratio = 1.0
    return display_currency, zoom_currency, float(zoom_ratio)


# 相容舊函式名（避免熱重載期間 NameError）
_resolve_zoom_currency = _resolve_currency_info


def _compute_buffer_amount_ccy(buffer_value, ratio, zoom_ratio=1.0) -> Optional[float]:
    """Buffer值金額(幣種) = BufferValue × DimMerchantInfo.Ratio × DimCurrencyInfoRelation.ZoomRatio"""
    bv = _safe_float(buffer_value)
    r = _safe_float(ratio)
    zr = _safe_float(zoom_ratio)
    if bv is None or r is None or zr is None:
        return None
    return float(bv) * float(r) * float(zr)


def _compute_twd_equivalent(
    buffer_value,
    merchant_id,
    merchant_map: Dict[int, Dict[str, Any]],
    rate_map: Dict[str, Dict[str, Any]],
    currency_relation_map: Dict[str, Dict[str, Any]],
) -> Optional[float]:
    """Buffer值金額(台幣) = Buffer值金額(幣種) × DimExchangeRateReport.Rate（ToCurrency=TWD, SourceType=accounting）"""
    if not merchant_map or not rate_map:
        return None
    mid = _safe_int(merchant_id)
    if mid is None:
        return None
    bv = _safe_float(buffer_value)
    if bv is None:
        return None
    m = merchant_map.get(mid)
    if not m:
        return None
    ratio = _safe_float(m.get("ratio"))
    credit_type = _safe_str(m.get("credit_type"))
    if ratio is None or not credit_type:
        return None
    display_currency, _, zoom_ratio = _resolve_currency_info(credit_type, currency_relation_map)
    ccy_amt = _compute_buffer_amount_ccy(bv, ratio, zoom_ratio)
    if ccy_amt is None:
        return None
    rate = _lookup_exchange_rate(display_currency, rate_map)
    if rate is None:
        return None
    return float(ccy_amt) * float(rate)


def _apply_twd_to_rows(rows, cfg: dict):
    merchant_map, rate_map, currency_relation_map = _get_bq_lookup_maps(cfg)
    for r in rows:
        mid = _safe_int(r.get("merchant_id"))
        m = merchant_map.get(mid) if mid is not None else None
        ratio = _safe_float(m.get("ratio")) if isinstance(m, dict) else None
        credit_type = _safe_str(m.get("credit_type")) if isinstance(m, dict) else ""
        display_currency, zoom_currency, zoom_ratio = _resolve_currency_info(
            credit_type, currency_relation_map
        )
        rate_entry = _lookup_rate_entry(display_currency, rate_map) if display_currency else None
        exchange_rate = _lookup_exchange_rate(display_currency, rate_map) if display_currency else None
        r["credit_type"] = credit_type
        r["currency"] = display_currency
        r["zoom_currency"] = zoom_currency
        r["zoom_ratio"] = zoom_ratio
        r["exchange_rate"] = exchange_rate
        r["exchange_rate_year"] = rate_entry.get("year") if rate_entry else None
        r["exchange_rate_month"] = rate_entry.get("month") if rate_entry else None
        r["exchange_rate_period_text"] = _format_exchange_rate_period(
            r["exchange_rate_year"],
            r["exchange_rate_month"],
        )

        ccy_amt = _compute_buffer_amount_ccy(r.get("buffer_value"), ratio, zoom_ratio)
        twd = _compute_twd_equivalent(
            r.get("buffer_value"),
            r.get("merchant_id"),
            merchant_map,
            rate_map,
            currency_relation_map,
        )

        r["merchant_ratio"] = ratio
        r["buffer_amount_ccy"] = ccy_amt
        r["buffer_amount_ccy_text"] = _format_twd_equivalent(ccy_amt)
        r["buffer_amount_twd"] = twd
        r["buffer_amount_twd_text"] = _format_twd_equivalent(twd)
        # 相容舊欄位名
        r["twd_equivalent"] = twd
        r["twd_equivalent_text"] = r["buffer_amount_twd_text"]


def _load_setting_rows(fish_db, include_disabled):
    setting_col = fish_db.get_collection("KioskBufferSetting")
    s_q = {}
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
    return setting_rows


def _load_game_map(gamedb_uri, gamedb_db, merchant_ids):
    game_map = {}
    if (not merchant_ids) or (not gamedb_uri):
        return game_map
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
        game_map.setdefault(mid, doc)
    return game_map


def _build_merchant_rows_from_values(values, setting_rows, game_map):
    # settings map for join
    setting_by_key = {}
    setting_by_merchant = {}
    default_settings = []
    for s in setting_rows:
        t = _safe_str(s.get("Theme"))
        m = _safe_str(s.get("Merchant"))
        c = _safe_str(s.get("Currency"))
        l = _safe_str(s.get("LineCode"))
        setting_by_key[(t, m, c, l)] = s
        setting_by_merchant.setdefault((t, m), s)
        if m.lower() == "default":
            default_settings.append(s)

    out = []
    for v in values:
        t = _safe_str(v.get("Theme"))
        m = _safe_str(v.get("Merchant"))
        c = _safe_str(v.get("Currency"))
        l = _safe_str(v.get("LineCode"))

        s = setting_by_key.get((t, m, c, l))
        if s is None:
            s = setting_by_merchant.get((t, m))
        if s is None:
            s = setting_by_key.get((t, "default", c, l))
        if s is None:
            s = setting_by_merchant.get((t, "default"))
        if s is None and default_settings:
            s = default_settings[0]

        mid = _safe_int(m)
        g = game_map.get(mid) if mid is not None else None
        ctrl_norm = s.get("ctrl_levels_norm") if isinstance(s, dict) else []
        if not isinstance(ctrl_norm, list):
            ctrl_norm = []
        hit = _compute_hit_stage(ctrl_norm, v.get("Value"))
        merchant_name = (g.get("merchant_name") if isinstance(g, dict) else None)

        out.append(
            {
                "theme": t,
                "merchant_id": mid,
                "merchant_raw": m,
                "currency": "",
                "credit_type": "",
                "zoom_currency": "",
                "zoom_ratio": None,
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
                "merchant_name": merchant_name,
            }
        )
    return out


def _handle_buffer_kiosk_info_data():
    """
    分頁資料 API：
    - 只撈「最新一筆」(Theme,Merchant,Currency,LineCode)
    - 只回 Value < 0
    - 依 Value 由小到大排序
    """
    db_env = (request.args.get("db_env", "macross-test") or "macross-test").strip()
    cfg = get_db_conn("BufferKioskInfoWeb", db_env, default_env="macross-test")
    fishdb_uri = str(cfg.get("fishdb_mongo_uri", "")).strip()
    fishdb_db = str(cfg.get("fishdb_db_name", "Buffer")).strip()
    gamedb_uri = str(cfg.get("gamedb_mongo_uri", "")).strip()
    gamedb_db = str(cfg.get("gamedb_db_name", "MainGame")).strip()

    include_disabled = (request.args.get("include_disabled", "0") or "0").strip() in ("1", "true", "True", "yes", "Y")
    theme = _safe_str(request.args.get("theme", ""))  # "" => all
    page = _safe_int(request.args.get("page", 1)) or 1
    page_size = _safe_int(request.args.get("page_size", 200)) or 200
    page = max(1, page)
    page_size = min(max(20, page_size), 500)

    if not fishdb_uri:
        return jsonify({"ok": False, "error": "尚未設定 fishdb 連線（FISHDB_MONGO_URI）。"}), 400

    fish_db = _get_client(fishdb_uri)[fishdb_db]
    value_col = fish_db.get_collection("KioskBufferValue")

    match_theme = {}
    if theme:
        match_theme = {"Theme": theme}

    skip = (page - 1) * page_size
    pipeline = [
        {"$match": match_theme} if match_theme else {"$match": {}},
        {"$sort": {"UpdateTime": -1}},
        {
            "$group": {
                "_id": {"Theme": "$Theme", "Merchant": "$Merchant", "Currency": "$Currency", "LineCode": "$LineCode"},
                "doc": {"$first": "$$ROOT"},
            }
        },
        {"$replaceRoot": {"newRoot": "$doc"}},
        {
            "$addFields": {
                "ValueNum": {
                    "$convert": {"input": "$Value", "to": "double", "onError": None, "onNull": None}
                }
            }
        },
        {"$match": {"ValueNum": {"$lt": 0}}},
        {"$sort": {"ValueNum": 1}},
        {
            "$facet": {
                "items": [
                    {"$skip": int(skip)},
                    {"$limit": int(page_size)},
                    {"$project": {"_id": 1, "Merchant": 1, "Theme": 1, "Currency": 1, "LineCode": 1, "UpdateTime": 1, "Value": 1}},
                ],
                "total": [{"$count": "n"}],
            }
        },
    ]

    agg = list(value_col.aggregate(pipeline, allowDiskUse=True))
    facet = (agg[0] if agg else {}) or {}
    items = facet.get("items", []) or []
    total_arr = facet.get("total", []) or []
    total = int(total_arr[0].get("n", 0)) if total_arr else 0

    # join: settings + gamedb
    setting_rows = _load_setting_rows(fish_db, include_disabled=include_disabled)
    merchant_ids = set()
    for v in items:
        mi = _safe_int(v.get("Merchant"))
        if mi is not None:
            merchant_ids.add(mi)
    game_map = _load_game_map(gamedb_uri=gamedb_uri, gamedb_db=gamedb_db, merchant_ids=merchant_ids)
    rows = _build_merchant_rows_from_values(items, setting_rows=setting_rows, game_map=game_map)
    _apply_twd_to_rows(rows, cfg)

    return jsonify(
        {
            "ok": True,
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_more": (skip + page_size) < total,
            "items": rows,
        }
    )


def _handle_buffer_kiosk_info(
    db_env_override=None,
    show_env_select_override=None,
    brand_title_override=None,
    brand_url_override=None,
    active_route_override=None,
):
    db_env = (db_env_override or (request.args.get("db_env", "macross-test") or "macross-test")).strip()
    cfg = get_db_conn("BufferKioskInfoWeb", db_env, default_env="macross-test")
    fishdb_uri = str(cfg.get("fishdb_mongo_uri", "")).strip()
    fishdb_db = str(cfg.get("fishdb_db_name", "Buffer")).strip()
    gamedb_uri = str(cfg.get("gamedb_mongo_uri", "")).strip()
    gamedb_db = str(cfg.get("gamedb_db_name", "MainGame")).strip()

    # theme:
    # - "" 表示「全部」（預設）
    # - 其他表示指定 theme
    selected_theme = _safe_str(request.args.get("theme", ""))
    include_disabled = (request.args.get("include_disabled", "0") or "0").strip() in ("1", "true", "True", "yes", "Y")
    show_all_merchants = (request.args.get("show_all", "0") or "0").strip() in ("1", "true", "True", "yes", "Y")
    q_api_name = _safe_str(request.args.get("api_name", ""))
    q_merchant_name = _safe_str(request.args.get("merchant_name", ""))
    q_merchant_id = _safe_str(request.args.get("merchant_id", ""))
    selected_activity = _safe_str(request.args.get("activity", "")).lower()
    if selected_activity not in ("", "1d", "7d", "30d"):
        selected_activity = ""
    refresh = (request.args.get("refresh", "0") or "0").strip() in ("1", "true", "True", "yes", "Y")

    warning = ""
    meta = ""

    theme_options = []
    setting_rows = []
    # 分頁模式：頁面本身不直接載入全部 value，改由前端呼叫 /KioskBufferInfo/data 分頁取得
    merchant_rows_all = []
    search_options = {"api_names": [], "merchant_names": []}

    if not fishdb_uri:
        warning = "尚未設定 fishdb 連線（FISHDB_MONGO_URI）。"
        return render_template(
            TEMPLATE_NAME,
            brand_title=(brand_title_override or "Kiosk Buffer 資訊"),
            brand_url=(brand_url_override or "/KioskBufferInfo"),
            active_route=(active_route_override or "kiosk_buffer_info"),
            show_env_select=(True if show_env_select_override is None else bool(show_env_select_override)),
            db_env=db_env,
            db_presets=[e for e in list_envs("BufferKioskInfoWeb") if e not in ("default", "macross-prod", "sssapi-prod")],
            warning=warning,
            meta="",
            theme_options=[],
            selected_theme=selected_theme,
            include_disabled=include_disabled,
            show_all_merchants=show_all_merchants,
            q_api_name=q_api_name,
            q_merchant_name=q_merchant_name,
            q_merchant_id=q_merchant_id,
            selected_activity=selected_activity,
            setting_rows=[],
            merchant_rows=[],
            search_options={"api_names": [], "merchant_names": []},
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

    # Theme 清單取不到或為空：直接顯示提示
    if (not theme_options):
        return render_template(
            TEMPLATE_NAME,
            brand_title=(brand_title_override or "Kiosk Buffer 資訊"),
            brand_url=(brand_url_override or "/KioskBufferInfo"),
            active_route=(active_route_override or "kiosk_buffer_info"),
            show_env_select=(True if show_env_select_override is None else bool(show_env_select_override)),
            db_env=db_env,
            db_presets=[e for e in list_envs("BufferKioskInfoWeb") if e not in ("default", "macross-prod", "sssapi-prod")],
            warning=warning,
            meta="尚無可用 Theme。",
            theme_options=theme_options,
            selected_theme="",
            include_disabled=include_disabled,
            show_all_merchants=show_all_merchants,
            q_api_name=q_api_name,
            q_merchant_name=q_merchant_name,
            q_merchant_id=q_merchant_id,
            selected_activity=selected_activity,
            setting_rows=[],
            merchant_rows=[],
            search_options={"api_names": [], "merchant_names": []},
            cache_status="SKIP",
        )

    return render_template(
        TEMPLATE_NAME,
        brand_title=(brand_title_override or "Kiosk Buffer 資訊"),
        brand_url=(brand_url_override or "/KioskBufferInfo"),
        active_route=(active_route_override or "kiosk_buffer_info"),
        show_env_select=(True if show_env_select_override is None else bool(show_env_select_override)),
        db_env=db_env,
        db_presets=[e for e in list_envs("BufferKioskInfoWeb") if e not in ("default", "macross-prod", "sssapi-prod")],
        warning=warning,
        meta=meta + (" | cache=BYPASS" if refresh else " | cache=MISS"),
        theme_options=theme_options,
        selected_theme=selected_theme,
        include_disabled=include_disabled,
        show_all_merchants=show_all_merchants,
        q_api_name=q_api_name,
        q_merchant_name=q_merchant_name,
        q_merchant_id=q_merchant_id,
        selected_activity=selected_activity,
        setting_rows=[],
        merchant_rows=[],
        merchant_rows_all=[],
        search_options=search_options,
        cache_status="PAGED",
    )


def init_buffer_kiosk_info_routes(flask_app):
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

    @flask_app.route("/KioskBufferInfo/data", methods=["GET"], endpoint="kiosk_buffer_info_data")
    def kiosk_buffer_info_data():  # noqa: F811
        return _handle_buffer_kiosk_info_data()

    @flask_app.route("/KioskBufferInfoMacrossProd", methods=["GET"], endpoint="kiosk_buffer_info_macross_prod")
    def kiosk_buffer_info_macross_prod():  # noqa: F811
        # 環境強制指定到 macross-prod，且不顯示 env select
        # connect_prod 由全站 gate 依 endpoint/db_env 自動判定（不依賴網址參數）
        return _handle_buffer_kiosk_info(
            db_env_override="macross-prod",
            show_env_select_override=False,
            brand_title_override="[Macross正式環境]Kiosk Buffer 資訊",
            brand_url_override="/KioskBufferInfoMacrossProd",
            active_route_override="kiosk_buffer_info_macross_prod",
        )

    @flask_app.route("/KioskBufferInfoSssapiProd", methods=["GET"], endpoint="kiosk_buffer_info_sssapi_prod")
    def kiosk_buffer_info_sssapi_prod():  # noqa: F811
        return _handle_buffer_kiosk_info(
            db_env_override="sssapi-prod",
            show_env_select_override=False,
            brand_title_override="[Sssapi正式環境]Kiosk Buffer 資訊",
            brand_url_override="/KioskBufferInfoSssapiProd",
            active_route_override="kiosk_buffer_info_sssapi_prod",
        )

