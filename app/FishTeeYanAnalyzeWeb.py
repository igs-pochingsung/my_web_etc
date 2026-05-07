"""
FishTeeYanAnalyzeWeb.py

用 Mongo 的 daily collections（例如 `FishStatisticsLog_20260423`）做玩家遊玩狀況分析圖：
- 主圖表：橫軸 = 時間、縱軸 = 資產（可選欄位/公式）
- 事件標記：贏大分 / 大倍（可調閥值）、打死特定魚（可設定清單）、collect 事件
- 篩選：日期（先以一天為單位）、玩家名稱（ArkID / UserID / NickName 皆可嘗試解析）

啟動（範例）
  set MONGO_URI=mongodb://localhost:27017
  set MONGO_DB=FishLog
  python Tools/FishTeeYanAnalyzeWeb.py
然後打開瀏覽器：`http://127.0.0.1:8799`
"""

from __future__ import annotations

import os
import time
import traceback
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote, urlencode

import pymongo
from flask import Flask, request, render_template

from DBConnect import get_db_conn, list_envs

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
except Exception as e:  # pragma: no cover
    raise SystemExit(
        "需要安裝 plotly。請先 `pip install plotly pymongo flask`。\n"
        f"import error: {e}"
    )


WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8799"))

# 時間輸入的時區偏移（小時）。你的 OS 顯示 UTC+8，預設用 8。
# 會把「日期+時間」先當成這個時區的時間，再換算成 UTC 去過濾資料（圖表 x 軸仍用 UTC）。
INPUT_TZ_OFFSET_HOURS = int(os.getenv("INPUT_TZ_OFFSET_HOURS", "8"))
PLAYTIME_SEARCH_DAYS = int(os.getenv("PLAYTIME_SEARCH_DAYS", "14"))

_MACROSS = get_db_conn("FishTeeYanAnalyzeWeb", "macross-test")
DEFAULT_MONGO_URI = str(_MACROSS.get("mongo_uri", "")).strip()
# 帳密請放在 MONGO_URI 內（例如 mongodb+srv://user:pwd@.../admin）。
# 不要再使用舊版 `authenticate()`（PyMongo 4 已移除，會導致 "Database object is not callable"）。
DEFAULT_MONGO_DB = str(_MACROSS.get("mongo_db", os.getenv("MONGO_DB", "BackendLog"))).strip()


# Mongo 連線預設：用 db_env 切換資料來源（供頁面上 bar 選單使用）
#
# - macross-test：原本的連線設定
# - sssapi-test：若設定了 SSSAPI_MONGO_URI 就直接用；否則嘗試把 DEFAULT_MONGO_URI 裡的
#   "macross-platdb-test" 取代成 "sssapi-platdb-test"
_SSSAPI = get_db_conn("FishTeeYanAnalyzeWeb", "sssapi-test")
SSSAPI_MONGO_URI = str(_SSSAPI.get("mongo_uri", DEFAULT_MONGO_URI)).strip()
# SSSAPI_MONGO_URI = os.getenv("SSSAPI_MONGO_URI", "").strip()
# if not SSSAPI_MONGO_URI and "macross-platdb-test" in DEFAULT_MONGO_URI:
#     SSSAPI_MONGO_URI = DEFAULT_MONGO_URI.replace("macross-platdb-test", "sssapi-platdb-test")
# if not SSSAPI_MONGO_URI:
#     SSSAPI_MONGO_URI = DEFAULT_MONGO_URI

DB_PRESETS: dict[str, dict[str, str]] = {}
for _env in list_envs("FishTeeYanAnalyzeWeb"):
    if _env == "default":
        continue
    _cfg = get_db_conn("FishTeeYanAnalyzeWeb", _env)
    if not _cfg:
        continue
    DB_PRESETS[_env] = {
        "mongo_uri": str(_cfg.get("mongo_uri", "")).strip(),
        "mongo_db": str(_cfg.get("mongo_db", DEFAULT_MONGO_DB)).strip(),
    }
if "macross-test" not in DB_PRESETS:
    DB_PRESETS["macross-test"] = {"mongo_uri": DEFAULT_MONGO_URI, "mongo_db": DEFAULT_MONGO_DB}

# 預先建立的 MongoDB handle（服務啟動時初始化兩套）
_MONGO_DBS: dict[str, Any] = {}

# 事件判定（可用環境變數覆寫，或直接改 code）
DEFAULT_BIG_WIN_THRESHOLD = float(os.getenv("BIG_WIN_THRESHOLD", "0"))  # 0 = disable
DEFAULT_BIG_MULT_THRESHOLD = float(os.getenv("BIG_MULT_THRESHOLD", "100"))  # 0 = disable
DEFAULT_SPECIAL_FISH_LIST = [
    s.strip()
    for s in os.getenv("SPECIAL_FISH_LIST", "").split(",")
    if s.strip()
]

# 資產顯示改為「資產變化」（累積 delta），不再使用 SessionGame(ValueBefore/After)
DEFAULT_ASSET_MODE = os.getenv("ASSET_MODE", "delta_asset")  # delta_asset


app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "fish-analyze")

# Weapon 顏色對照（同圖表 weapon lanes）
_WEAPON_COLOR_MAP = {
    "fast": "#c4b5fd",  # light purple
    "lock": "#5b21b6",  # deep purple
    "redlock": "#5b21b6",  # deep purple
    "tiger": "#f59e0b",  # orange
    "phoenix": "#ef4444",  # red
}


def _weapon_color_hex(weapon: Any) -> str:
    key = (str(weapon) if weapon is not None else "-").strip().lower()
    if key in _WEAPON_COLOR_MAP:
        return _WEAPON_COLOR_MAP[key]
    # fallback stable-ish palette by hash
    palette = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f"]
    i = abs(hash(key))
    return palette[i % len(palette)]


def _text_color_for_bg(hex_color: str) -> str:
    """Return black/white text color for given hex background."""
    c = hex_color.lstrip("#")
    if len(c) != 6:
        return "#111827"
    r = int(c[0:2], 16)
    g = int(c[2:4], 16)
    b = int(c[4:6], 16)
    # relative luminance
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#111827" if lum > 160 else "#ffffff"

# 5 分鐘記憶體快取（同條件查詢秒回）
CACHE_TTL_SEC = int(os.getenv("CACHE_TTL_SEC", "300"))
_QUERY_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}


def _cache_get(key: tuple[Any, ...]) -> Optional[dict[str, Any]]:
    now = time.time()
    # 簡單清理過期
    expired = [k for k, (exp, _) in _QUERY_CACHE.items() if exp <= now]
    for k in expired:
        _QUERY_CACHE.pop(k, None)
    hit = _QUERY_CACHE.get(key)
    if not hit:
        return None
    exp, payload = hit
    if exp <= now:
        _QUERY_CACHE.pop(key, None)
        return None
    return payload


def _cache_set(key: tuple[Any, ...], payload: dict[str, Any]) -> None:
    _QUERY_CACHE[key] = (time.time() + CACHE_TTL_SEC, payload)


def _to_date_key(date_str: str) -> str:
    # input: YYYY-MM-DD -> YYYYMMDD
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return d.strftime("%Y%m%d")


def _build_time_range_utc(date_str: str, start_hhmm: str, end_hhmm: str) -> tuple[datetime, datetime]:
    """
    將 YYYY-MM-DD + HH:MM 轉成 UTC datetime 範圍（含頭含尾）。
    注意：時間輸入先以 INPUT_TZ_OFFSET_HOURS 解讀（例如 UTC+8），再轉換為 UTC（和圖表 x 軸一致）。
    """
    base_local = datetime.strptime(date_str, "%Y-%m-%d").replace(
        tzinfo=timezone(timedelta(hours=INPUT_TZ_OFFSET_HOURS))
    )
    sh, sm = [int(x) for x in (start_hhmm or "00:00").split(":")[:2]]
    eh, em = [int(x) for x in (end_hhmm or "23:59").split(":")[:2]]
    start_local = base_local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end_local = base_local.replace(hour=eh, minute=em, second=59, microsecond=999000)
    start_dt = start_local.astimezone(timezone.utc)
    end_dt = end_local.astimezone(timezone.utc)
    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt
    return start_dt, end_dt


def _dt_utc_to_local(dt_utc: datetime) -> datetime:
    return dt_utc.astimezone(timezone(timedelta(hours=INPUT_TZ_OFFSET_HOURS)))


def _round_down_to_10min(dt_local: datetime) -> datetime:
    m = (dt_local.minute // 10) * 10
    return dt_local.replace(minute=m, second=0, microsecond=0)


def _round_up_to_10min(dt_local: datetime) -> datetime:
    # ceil to next 10-min boundary (inclusive if already on boundary)
    if dt_local.second == 0 and dt_local.microsecond == 0 and dt_local.minute % 10 == 0:
        return dt_local
    add = 10 - (dt_local.minute % 10)
    base = dt_local.replace(second=0, microsecond=0)
    return base + timedelta(minutes=add)


def _dt_local_to_hhmm(dt_local: datetime) -> str:
    return dt_local.strftime("%H:%M")


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def _ts_to_dt_utc(ts_seconds: int) -> datetime:
    return datetime.fromtimestamp(int(ts_seconds), tz=timezone.utc)


def _ts_ms_to_dt_utc(ts_milliseconds: int) -> datetime:
    return datetime.fromtimestamp(int(ts_milliseconds) / 1000.0, tz=timezone.utc)


def _ts_us_to_dt_utc(ts_microseconds: int) -> datetime:
    # 例如 FishPlayerInOutTime: EnterTs/LeaveTs 是 microseconds
    return datetime.fromtimestamp(int(ts_microseconds) / 1_000_000, tz=timezone.utc)


@dataclass(frozen=True)
class PlayerIdentity:
    ark_id: Optional[str]
    user_id: Optional[str]
    nickname: Optional[str]


def _build_mongo(uri: str, db_name: str):
    # mongodb+srv 需要 dnspython
    if uri.strip().startswith("mongodb+srv://"):
        try:
            import dns  # type: ignore  # noqa: F401
        except Exception:
            raise RuntimeError("mongodb+srv 需要 dnspython：請先 `py -3 -m pip install dnspython`")

    if "<pwd>" in uri:
        raise RuntimeError("請在 MONGO_URI 放入正確密碼（把 <pwd> 換掉），或用環境變數設定。")

    # MongoClient 本身是 thread-safe 的，做全域快取可大幅降低每次查詢的選主/握手成本
    from pymongo.errors import ServerSelectionTimeoutError

    global _MONGO_CLIENT_CACHE  # noqa: PLW0603
    try:
        _MONGO_CLIENT_CACHE
    except NameError:
        _MONGO_CLIENT_CACHE = {}  # type: ignore

    cached = _MONGO_CLIENT_CACHE.get(uri)
    if cached is None:
        # 用較短的 server selection timeout，避免網頁卡住太久
        client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=15000, connectTimeoutMS=10000)
        try:
            client.admin.command("ping")
        except ServerSelectionTimeoutError as e:
            raise RuntimeError(
                "Mongo 連線逾時（ServerSelectionTimeoutError）。\n"
                "- 請確認 Atlas IP Access List 有放行你目前的外網 IP\n"
                "- 或公司網路/防火牆是否擋 27017 出站\n"
                "- replicaSet / authMechanism 參數是否正確\n"
                f"detail: {e}"
            )
        _MONGO_CLIENT_CACHE[uri] = client
        cached = client

    return cached[db_name]


def _resolve_players(mdb, date_key: str, player_query: str) -> list[PlayerIdentity]:
    """
    先用 `FishPlayerInOutTime_YYYYMMDD` 解析玩家（ArkID / UserID / NickName）。

    - 如果輸入 ArkID/UserID/MerchantUserAccount 命中：回傳單一玩家
    - 如果輸入 NickName 命中多筆：依 ArkID 分組，回傳多個玩家（同頁畫多張圖）
    - 找不到：回傳一筆只帶 nickname=query（讓使用者看到查詢值）
    """
    col_name = f"FishPlayerInOutTime_{date_key}"
    col = mdb.get_collection(col_name)

    q = (player_query or "").strip()
    if not q:
        return [PlayerIdentity(ark_id=None, user_id=None, nickname=None)]

    # 先走「精準欄位」：ArkID / UserID / MerchantUserAccount
    exact = col.find_one(
        {"$or": [{"ArkID": q}, {"UserID": q}, {"MerchantUserAccount": q}]},
        {"_id": 0, "ArkID": 1, "UserID": 1, "NickName": 1},
        sort=[("CreateTs", pymongo.DESCENDING)],
    )
    if exact:
        return [
            PlayerIdentity(
                ark_id=str(exact.get("ArkID")) if exact.get("ArkID") is not None else None,
                user_id=str(exact.get("UserID")) if exact.get("UserID") is not None else None,
                nickname=str(exact.get("NickName")) if exact.get("NickName") is not None else None,
            )
        ]

    # NickName 可能對到多個 ArkID：依 ArkID group by，取每個 ArkID 最新的一筆
    cursor = col.find(
        {"NickName": q},
        {"_id": 0, "ArkID": 1, "UserID": 1, "NickName": 1, "CreateTs": 1},
    ).sort("CreateTs", pymongo.DESCENDING)

    by_ark: dict[str, dict[str, Any]] = {}
    for doc in cursor:
        ark = doc.get("ArkID")
        if ark is None:
            continue
        ark_s = str(ark)
        if ark_s not in by_ark:
            by_ark[ark_s] = doc
        if len(by_ark) >= 20:
            break

    if by_ark:
        out: list[PlayerIdentity] = []
        for ark_s, doc in sorted(by_ark.items(), key=lambda kv: kv[0]):
            out.append(
                PlayerIdentity(
                    ark_id=ark_s,
                    user_id=str(doc.get("UserID")) if doc.get("UserID") is not None else None,
                    nickname=str(doc.get("NickName")) if doc.get("NickName") is not None else None,
                )
            )
        return out

    return [PlayerIdentity(ark_id=None, user_id=None, nickname=q)]


def _asset_value_from_stats(doc: dict[str, Any], mode: str) -> Optional[float]:
    mode = (mode or "").strip().lower()
    total_win = _safe_float(doc.get("TotalWin"))
    total_bet = _safe_float(doc.get("TotalBet"))
    buffer_sub = _safe_float(doc.get("BufferSub"))

    if mode == "total_win":
        return total_win
    if mode == "buffer_sub":
        return buffer_sub
    # default: net
    if total_win is None or total_bet is None:
        return None
    return total_win - total_bet


def _parse_fish_ids_from_temptext(temp_text: Any) -> list[str]:
    """
    SessionGame.TempText 例： "LOBSTER,40,320,Lock;"
    可能是多段用 ';' 分隔。這裡抓每段第一個 token 當 fish id。
    """
    if not temp_text:
        return []
    s = str(temp_text)
    parts = [p.strip() for p in s.split(";") if p.strip()]
    out: list[str] = []
    for p in parts:
        first = p.split(",")[0].strip()
        if first:
            out.append(first.upper())
    return out


def _load_detail_betwin_fish_raw_series(
    mdb,
    date_key: str,
    player: PlayerIdentity,
    start_us_utc: Optional[int] = None,
    end_us_utc: Optional[int] = None,
) -> list[dict[str, Any]]:
    """
    以 DetailBetWinFishRaw 直接建立主序列（完全不依賴 SessionGame/GameSession）：
    - asset = 累積資產變化（sum(WinAmount - BetAmount)）
    """
    col_name = f"DetailBetWinFishRaw_{date_key}"
    col = mdb.get_collection(col_name)

    q: dict[str, Any] = {}
    if player.ark_id:
        q["ArkID"] = player.ark_id
    elif player.user_id:
        q["UserID"] = player.user_id
    elif player.nickname:
        # 盡量相容，但若該 collection 沒有 NickName 欄位會查不到（仍可用 ark_id/user_id）
        q["NickName"] = player.nickname
    else:
        return []

    if start_us_utc is not None or end_us_utc is not None:
        r: dict[str, Any] = {}
        if start_us_utc is not None:
            r["$gte"] = int(start_us_utc)
        if end_us_utc is not None:
            r["$lte"] = int(end_us_utc)
        q["CreateTs"] = r

    cursor = col.find(
        q,
        {
            "_id": 0,
            "WagersID": 1,
            "CreateTs": 1,
            "Weapon": 1,
            "BetScale": 1,
            "OriginalBet": 1,
            "Target": 1,
            "WinAmount": 1,
            "WinCount": 1,
            "BetAmount": 1,
            "BetCount": 1,
        },
    ).sort("CreateTs", pymongo.ASCENDING)

    series: list[dict[str, Any]] = []
    cum = 0.0
    for doc in cursor:
        ts = doc.get("CreateTs")
        if ts is None:
            continue
        try:
            t = _ts_us_to_dt_utc(int(ts))
        except Exception:
            continue
        wid = doc.get("WagersID")
        try:
            wid_i = int(wid) if wid is not None else None
        except Exception:
            wid_i = None

        wa = _safe_float(doc.get("WinAmount")) or 0.0
        ba = _safe_float(doc.get("BetAmount")) or 0.0
        delta = wa - ba
        cum += float(delta)

        wc = doc.get("WinCount")
        try:
            wc_i = int(wc) if wc is not None else 0
        except Exception:
            wc_i = 0
        ob = _safe_float(doc.get("OriginalBet"))
        mult = None
        if wc_i > 0 and ob is not None and ob > 0:
            mult = (wa / wc_i) / ob

        series.append(
            {
                "t": t,
                "asset": cum,
                "asset_delta": delta,
                "wagers_id": wid_i,
                "win_amount": wa,
                "effect_bet": ba,
                "mult": mult,
                "fish_ids": [str(doc.get("Target") or "-").upper()],
                "raw_targets": [str(doc.get("Target") or "-")],
                "weapon": str(doc.get("Weapon") or "-"),
                "bet_segment": ob,
            }
        )
    return series


def _load_detail_betwin_fish_raw_map(
    mdb,
    date_key: str,
    player: PlayerIdentity,
    wagers_ids: list[int],
    start_us_utc: Optional[int] = None,
    end_us_utc: Optional[int] = None,
) -> dict[int, dict[str, Any]]:
    """
    從 DetailBetWinFishRaw_YYYYMMDD 讀取資料，依 WagersID 聚合：
    - weapon_set: set[str]
    - bet_scale_set: set[int]
    - target_set: set[str]
    """
    if not wagers_ids:
        return {}

    col_name = f"DetailBetWinFishRaw_{date_key}"
    col = mdb.get_collection(col_name)

    q: dict[str, Any] = {"WagersID": {"$in": wagers_ids}}
    # 盡量加上玩家條件縮小範圍
    if player.ark_id:
        q["ArkID"] = player.ark_id
    elif player.user_id:
        q["UserID"] = player.user_id

    if start_us_utc is not None or end_us_utc is not None:
        r: dict[str, Any] = {}
        if start_us_utc is not None:
            r["$gte"] = int(start_us_utc)
        if end_us_utc is not None:
            r["$lte"] = int(end_us_utc)
        q["CreateTs"] = r

    cursor = col.find(
        q,
        {
            "_id": 0,
            "WagersID": 1,
            "Weapon": 1,
            "BetScale": 1,
            "Target": 1,
            "CreateTs": 1,
            "BetAmount": 1,
            "WinAmount": 1,
            "WinCount": 1,
            "BetCount": 1,
            "OriginalBet": 1,
        },
    )

    out: dict[int, dict[str, Any]] = {}
    for doc in cursor:
        wid = doc.get("WagersID")
        if wid is None:
            continue
        try:
            wid_i = int(wid)
        except Exception:
            continue

        agg = out.setdefault(
            wid_i,
            {
                "weapon_set": set(),
                "bet_scale_set": set(),
                "original_bet_set": set(),
                "target_set": set(),
                "raw_count": 0,
                "sum_win_amount": 0.0,
                "sum_win_count": 0,
                "sum_bet_amount": 0.0,
            },
        )
        agg["raw_count"] += 1

        w = doc.get("Weapon")
        if w:
            agg["weapon_set"].add(str(w))
        bs = doc.get("BetScale")
        if bs is not None:
            try:
                agg["bet_scale_set"].add(int(bs))
            except Exception:
                pass
        ob = doc.get("OriginalBet")
        if ob is not None:
            obf = _safe_float(ob)
            if obf is not None:
                agg["original_bet_set"].add(float(obf))
        tg = doc.get("Target")
        if tg:
            agg["target_set"].add(str(tg))

        agg["sum_win_amount"] += _safe_float(doc.get("WinAmount")) or 0.0
        wc = doc.get("WinCount")
        try:
            agg["sum_win_count"] += int(wc) if wc is not None else 0
        except Exception:
            pass
        agg["sum_bet_amount"] += _safe_float(doc.get("BetAmount")) or 0.0

    return out


def _load_detail_betwin_fish_raw_events(
    mdb,
    date_key: str,
    player: PlayerIdentity,
    wagers_ids: list[int],
    big_win_threshold: float,
    big_mult_threshold: float,
    special_fish_list: list[str],
    start_us_utc: Optional[int] = None,
    end_us_utc: Optional[int] = None,
) -> list[dict[str, Any]]:
    """
    事件改以「單筆 DetailBetWinFishRaw」計算，不做聚合。
    - big win: WinAmount >= threshold
    - big mult: (WinAmount/WinCount)/OriginalBet >= threshold
    - special fish: Target 在清單內
    """
    if not wagers_ids:
        return []

    col_name = f"DetailBetWinFishRaw_{date_key}"
    col = mdb.get_collection(col_name)

    q: dict[str, Any] = {"WagersID": {"$in": wagers_ids}}
    if player.ark_id:
        q["ArkID"] = player.ark_id
    elif player.user_id:
        q["UserID"] = player.user_id

    if start_us_utc is not None or end_us_utc is not None:
        r: dict[str, Any] = {}
        if start_us_utc is not None:
            r["$gte"] = int(start_us_utc)
        if end_us_utc is not None:
            r["$lte"] = int(end_us_utc)
        q["CreateTs"] = r

    cursor = col.find(
        q,
        {
            "_id": 0,
            "WagersID": 1,
            "CreateTs": 1,
            "Weapon": 1,
            "BetScale": 1,
            "OriginalBet": 1,
            "Target": 1,
            "WinAmount": 1,
            "WinCount": 1,
            "BetAmount": 1,
        },
    ).sort("CreateTs", pymongo.ASCENDING)

    special_set = {s.strip().upper() for s in special_fish_list if s.strip()}
    ev: list[dict[str, Any]] = []

    for doc in cursor:
        ts = doc.get("CreateTs")
        if ts is None:
            continue
        try:
            t = _ts_us_to_dt_utc(int(ts))
        except Exception:
            continue

        target = str(doc.get("Target") or "-")
        weapon = str(doc.get("Weapon") or "-")
        bet_scale = doc.get("BetScale")
        ob = _safe_float(doc.get("OriginalBet"))
        wa = _safe_float(doc.get("WinAmount"))
        wc = doc.get("WinCount")
        try:
            wc_i = int(wc) if wc is not None else 0
        except Exception:
            wc_i = 0

        # special fish event (Target)
        if target and target != "-" and target.upper() in special_set:
            ev.append({"t": t, "kind": "special_fish", "label": f"special fish: {target}<br>weapon={weapon}"})

        # big win event (threshold==0 means disable)
        if big_win_threshold > 0 and wa is not None and wa >= big_win_threshold:
            ev.append(
                {
                    "t": t,
                    "kind": "big_win",
                    "label": f"big win: {wa:.2f} (>= {big_win_threshold:.2f})<br>target={target}<br>weapon={weapon}",
                }
            )

        # big mult event
        mult = None
        if wa is not None and wc_i > 0 and ob is not None and ob > 0:
            mult = (wa / wc_i) / ob
        # big mult event (threshold==0 means disable)
        if big_mult_threshold > 0 and mult is not None and mult >= big_mult_threshold:
            ev.append(
                {
                    "t": t,
                    "kind": "big_mult",
                    "label": f"big mult: x{mult:.2f} (>= x{big_mult_threshold:.2f})<br>target={target}<br>weapon={weapon}<br>OriginalBet={ob}",
                }
            )

    return ev


def _load_detail_betwin_fish_raw_rows(
    mdb,
    date_key: str,
    player: PlayerIdentity,
    wagers_ids: list[int],
    start_us_utc: Optional[int] = None,
    end_us_utc: Optional[int] = None,
) -> list[dict[str, Any]]:
    """載入單筆 DetailBetWinFishRaw rows（供 weapon / OriginalBet 圖與事件使用）。"""
    if not wagers_ids:
        return []

    col_name = f"DetailBetWinFishRaw_{date_key}"
    col = mdb.get_collection(col_name)

    q: dict[str, Any] = {"WagersID": {"$in": wagers_ids}}
    if player.ark_id:
        q["ArkID"] = player.ark_id
    elif player.user_id:
        q["UserID"] = player.user_id

    if start_us_utc is not None or end_us_utc is not None:
        r: dict[str, Any] = {}
        if start_us_utc is not None:
            r["$gte"] = int(start_us_utc)
        if end_us_utc is not None:
            r["$lte"] = int(end_us_utc)
        q["CreateTs"] = r

    cursor = col.find(
        q,
        {
            "_id": 0,
            "WagersID": 1,
            "CreateTs": 1,
            "Weapon": 1,
            "OriginalBet": 1,
            "BetScale": 1,
            "Target": 1,
            "WinAmount": 1,
            "WinCount": 1,
            "BetAmount": 1,
            "BetCount": 1,
        },
    ).sort("CreateTs", pymongo.ASCENDING)

    out: list[dict[str, Any]] = []
    for doc in cursor:
        ts = doc.get("CreateTs")
        if ts is None:
            continue
        try:
            t = _ts_us_to_dt_utc(int(ts))
        except Exception:
            continue
        wid = doc.get("WagersID")
        try:
            wid_i = int(wid) if wid is not None else None
        except Exception:
            wid_i = None
        out.append(
            {
                "t": t,
                "wagers_id": wid_i,
                "weapon": str(doc.get("Weapon") or "-"),
                "original_bet": _safe_float(doc.get("OriginalBet")),
                "bet_scale": doc.get("BetScale"),
                "target": str(doc.get("Target") or "-"),
                "win_amount": _safe_float(doc.get("WinAmount")),
                "win_count": int(doc.get("WinCount") or 0),
                "bet_amount": _safe_float(doc.get("BetAmount")),
                "bet_count": int(doc.get("BetCount") or 0),
            }
        )
    return out


def _raw_rows_to_events(
    raw_rows: list[dict[str, Any]],
    big_win_threshold: float,
    big_mult_threshold: float,
    special_fish_list: list[str],
) -> list[dict[str, Any]]:
    """把單筆 DetailBetWinFishRaw rows 轉成事件點（不聚合）。"""
    special_set = {s.strip().upper() for s in special_fish_list if s.strip()}
    ev: list[dict[str, Any]] = []

    for r in raw_rows:
        t = r["t"]
        target = str(r.get("target") or "-")
        weapon = str(r.get("weapon") or "-")
        ob = _safe_float(r.get("original_bet"))
        wa = _safe_float(r.get("win_amount"))
        wc_i = int(r.get("win_count") or 0)

        # special fish event
        if target and target != "-" and target.upper() in special_set:
            ev.append({"t": t, "kind": "special_fish", "label": f"special fish: {target}<br>weapon={weapon}"})

        # big win event (threshold==0 means disable)
        if big_win_threshold > 0 and wa is not None and wa >= big_win_threshold:
            ev.append(
                {
                    "t": t,
                    "kind": "big_win",
                    "label": f"big win: {wa:.2f} (>= {big_win_threshold:.2f})<br>target={target}<br>weapon={weapon}",
                }
            )

        # big mult event (threshold==0 means disable)
        mult = None
        if wa is not None and wc_i > 0 and ob is not None and ob > 0:
            mult = (wa / wc_i) / ob
        if big_mult_threshold > 0 and mult is not None and mult >= big_mult_threshold:
            ev.append(
                {
                    "t": t,
                    "kind": "big_mult",
                    "label": f"big mult: x{mult:.2f} (>= x{big_mult_threshold:.2f})<br>target={target}<br>weapon={weapon}<br>OriginalBet={ob}",
                }
            )

    return ev


def _intersect_intervals(
    a: list[tuple[datetime, datetime]],
    b: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """回傳兩組已排序/可重疊區間的交集（結果會 merge）。"""
    if not a or not b:
        return []
    a2 = sorted(a, key=lambda p: p[0])
    b2 = sorted(b, key=lambda p: p[0])
    i = 0
    j = 0
    out: list[tuple[datetime, datetime]] = []
    while i < len(a2) and j < len(b2):
        s = max(a2[i][0], b2[j][0])
        e = min(a2[i][1], b2[j][1])
        if e > s:
            if not out or s > out[-1][1]:
                out.append((s, e))
            else:
                out[-1] = (out[-1][0], max(out[-1][1], e))
        if a2[i][1] < b2[j][1]:
            i += 1
        else:
            j += 1
    return out


def _rebase_asset_series_by_in_game(
    series: list[dict[str, Any]],
    in_game_intervals_utc: list[tuple[datetime, datetime]],
) -> list[dict[str, Any]]:
    """
    把「資產變化」以每段 in-game 區間為基準重新歸零：
    - 每次進入遊戲（interval start）都視為資產變化 = 0
    - 只在 in-game 期間累積 delta（WinAmount - BetAmount）
    - 不在 in-game 的點會把 asset 設為 None（避免把不同段落連起來）
    """
    if not series or not in_game_intervals_utc:
        return series

    ig = sorted(in_game_intervals_utc, key=lambda p: p[0])

    def interval_for(t: datetime) -> Optional[tuple[datetime, datetime]]:
        # ig 已 merge 且排序，可以用線性掃描（資料量通常不大）
        for s, e in ig:
            if s <= t <= e:
                return (s, e)
        return None

    out: list[dict[str, Any]] = []
    current_interval: Optional[tuple[datetime, datetime]] = None
    cum = 0.0
    inserted_zero_for: set[datetime] = set()

    for r in series:
        t = r.get("t")
        if not isinstance(t, datetime):
            continue

        iv = interval_for(t)
        if iv is None:
            r2 = dict(r)
            r2["asset"] = None
            out.append(r2)
            continue

        # 進入新的 in-game interval：先插入起點 asset=0
        if current_interval is None or iv[0] != current_interval[0]:
            current_interval = iv
            cum = 0.0
            if iv[0] not in inserted_zero_for:
                inserted_zero_for.add(iv[0])
                out.append(
                    {
                        "t": iv[0],
                        "asset": 0.0,
                        "asset_delta": 0.0,
                        "wagers_id": None,
                        "win_amount": 0.0,
                        "effect_bet": 0.0,
                        "mult": None,
                        "fish_ids": [],
                        "raw_targets": [],
                        "weapon": "-",
                        "bet_segment": None,
                    }
                )

        delta = _safe_float(r.get("asset_delta")) or 0.0
        cum += float(delta)
        r2 = dict(r)
        r2["asset"] = cum
        out.append(r2)

    out.sort(key=lambda x: x.get("t", datetime.min.replace(tzinfo=timezone.utc)))
    return out


def _sum_interval_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    return float(sum(max(0.0, (e - s).total_seconds()) for s, e in intervals))


def _compute_ratios_html(
    raw_rows: list[dict[str, Any]],
    in_game_intervals_utc: list[tuple[datetime, datetime]],
    start_dt_utc: datetime,
    end_dt_utc: datetime,
) -> str:
    """
    顯示：
    - 單筆押注比例（依 OriginalBet）
    - 武器使用比例（依時間）
    - 打魚行為佔遊戲中比例（秒數）
    """
    # clamp in-game to query range
    query_win = [(start_dt_utc, end_dt_utc)]
    in_game = _intersect_intervals(in_game_intervals_utc, query_win)
    in_game_sec = _sum_interval_seconds(in_game)

    # raw rows in range + in game
    raw_sorted = sorted([r for r in raw_rows if start_dt_utc <= r["t"] <= end_dt_utc], key=lambda r: r["t"])
    raw_g = [r for r in raw_sorted if any(s <= r["t"] <= e for s, e in in_game)] if in_game else []

    # OriginalBet ratio by BetCount
    ob_counts: dict[str, int] = {}
    total_ob = 0
    for r in raw_g:
        ob = r.get("original_bet")
        if ob is None:
            continue
        bc = int(r.get("bet_count") or 0)
        if bc <= 0:
            continue
        key = f"{float(ob):g}"
        ob_counts[key] = ob_counts.get(key, 0) + bc
        total_ob += bc
    ob_items = sorted(ob_counts.items(), key=lambda kv: (-kv[1], kv[0]))

    # Weapon usage ratio by BetCount (依次數)
    weapon_cnt: dict[str, int] = {}
    total_weapon_cnt = 0
    for r in raw_g:
        w = str(r.get("weapon") or "-")
        bc = int(r.get("bet_count") or 0)
        if bc <= 0:
            continue
        weapon_cnt[w] = weapon_cnt.get(w, 0) + bc
        total_weapon_cnt += bc
    weapon_items = sorted(weapon_cnt.items(), key=lambda kv: (-kv[1], kv[0]))

    # Shooting ratio: each raw row => [t, t+3s], merge then intersect in-game
    shoot_windows: list[tuple[datetime, datetime]] = []
    for r in raw_g:
        s = r["t"]
        shoot_windows.append((s, min(s + timedelta(seconds=3), end_dt_utc)))
    shoot_windows.sort(key=lambda p: p[0])
    merged: list[tuple[datetime, datetime]] = []
    for s, e in shoot_windows:
        if not merged or s > merged[-1][1]:
            merged.append((s, e))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
    shoot_in_game = _intersect_intervals(merged, in_game)
    shoot_sec = _sum_interval_seconds(shoot_in_game)
    shoot_ratio = (shoot_sec / in_game_sec * 100.0) if in_game_sec > 0 else 0.0

    def pct(n: float, d: float) -> str:
        return f"{(n / d * 100.0):.1f}%" if d > 0 else "0.0%"

    # Build small HTML (Bootstrap)
    ob_html = (
        "<div class='small text-secondary mb-1'>單筆押注比例（依 BetCount）</div>"
        + ("<div class='small text-muted'>無資料</div>" if total_ob == 0 else
           "<div class='d-flex flex-wrap gap-2'>"
           + "".join(
               f"<span class='badge text-bg-light border'>OB {k}: {v} ({pct(v, total_ob)})</span>"
               for k, v in ob_items[:12]
           )
           + "</div>")
    )
    weapon_html = (
        "<div class='small text-secondary mt-2 mb-1'>武器使用比例（依 BetCount）</div>"
        + ("<div class='small text-muted'>無資料</div>" if total_weapon_cnt == 0 else
           "<div class='d-flex flex-wrap gap-2'>"
           + "".join(
               (
                   f"<span class='badge border' style='background:{_weapon_color_hex(w)}; color:{_text_color_for_bg(_weapon_color_hex(w))}'>"
                   f"{w}: {cnt} ({pct(cnt, total_weapon_cnt)})</span>"
               )
               for w, cnt in weapon_items[:12]
           )
           + "</div>")
    )
    shoot_bg = "#16a34a"
    shoot_html = (
        "<div class='small text-secondary mt-2 mb-1'>打魚行為佔遊戲中比例</div>"
        + (
            "<div class='small'>"
            f"<span class='badge border' style='background:{shoot_bg}; color:{_text_color_for_bg(shoot_bg)}'>"
            f"shooting {shoot_sec:.0f}s / in-game {in_game_sec:.0f}s = {shoot_ratio:.1f}%</span>"
            "</div>"
        )
    )
    return f"<div class='mb-2'>{ob_html}{weapon_html}{shoot_html}</div>"


def _load_transaction_fish_log_map(
    mdb,
    date_key: str,
    player: PlayerIdentity,
    wagers_ids: list[int],
    start_us_utc: Optional[int] = None,
    end_us_utc: Optional[int] = None,
) -> dict[int, dict[str, Any]]:
    """
    從 TransactionFishLog_YYYYMMDD 讀取資料，依 WagersID 聚合（同一 WagersID 可能多筆）。
    用來取代 SessionGame 的 bet/win 計算來源。
    """
    if not wagers_ids:
        return {}

    col_name = f"TransactionFishLog_{date_key}"
    col = mdb.get_collection(col_name)

    q: dict[str, Any] = {"WagersID": {"$in": wagers_ids}}
    if player.ark_id:
        q["ArkID"] = player.ark_id
    elif player.user_id:
        q["UserID"] = player.user_id

    if start_us_utc is not None or end_us_utc is not None:
        r: dict[str, Any] = {}
        if start_us_utc is not None:
            r["$gte"] = int(start_us_utc)
        if end_us_utc is not None:
            r["$lte"] = int(end_us_utc)
        q["CreateTs"] = r

    cursor = col.find(
        q,
        {
            "_id": 0,
            "WagersID": 1,
            "BetAmount": 1,
            "WinAmount": 1,
            "BetCount": 1,
            "WinCount": 1,
            "BetType": 1,
            "CreateTs": 1,
        },
    )

    out: dict[int, dict[str, Any]] = {}
    for doc in cursor:
        wid = doc.get("WagersID")
        if wid is None:
            continue
        try:
            wid_i = int(wid)
        except Exception:
            continue

        agg = out.setdefault(
            wid_i,
            {
                "bet_amount": 0.0,
                "win_amount": 0.0,
                "bet_count": 0,
                "win_count": 0,
                "bet_type_set": set(),
                "row_count": 0,
            },
        )
        agg["row_count"] += 1
        ba = _safe_float(doc.get("BetAmount")) or 0.0
        wa = _safe_float(doc.get("WinAmount")) or 0.0
        agg["bet_amount"] += ba
        agg["win_amount"] += wa
        bc = doc.get("BetCount")
        wc = doc.get("WinCount")
        try:
            agg["bet_count"] += int(bc) if bc is not None else 0
        except Exception:
            pass
        try:
            agg["win_count"] += int(wc) if wc is not None else 0
        except Exception:
            pass
        bt = doc.get("BetType")
        if bt:
            agg["bet_type_set"].add(str(bt))

    return out


def _load_collect_events(
    mdb,
    date_key: str,
    player: PlayerIdentity,
    start_s_utc: Optional[int] = None,
    end_s_utc: Optional[int] = None,
) -> list[dict[str, Any]]:
    col_name = f"FishCollectDetailLog_{date_key}"
    col = mdb.get_collection(col_name)

    if not player.ark_id:
        return []

    q: dict[str, Any] = {"ArkID": player.ark_id}
    if start_s_utc is not None or end_s_utc is not None:
        r: dict[str, Any] = {}
        if start_s_utc is not None:
            r["$gte"] = int(start_s_utc)
        if end_s_utc is not None:
            r["$lte"] = int(end_s_utc)
        q["CreateTimeTS"] = r

    cursor = col.find(
        q,
        {"_id": 0, "CreateTimeTS": 1, "EventType": 1, "ItemName": 1, "ItemAmount": 1, "ActionType": 1},
    ).sort("CreateTimeTS", pymongo.ASCENDING)

    ev: list[dict[str, Any]] = []
    for doc in cursor:
        ts = doc.get("CreateTimeTS")
        if ts is None:
            continue
        ev.append(
            {
                "t": _ts_to_dt_utc(int(ts)),
                "kind": "collect",
                "label": f"{doc.get('ActionType','')} {doc.get('EventType','')} {doc.get('ItemName','')} x{doc.get('ItemAmount', 1)}".strip(),
            }
        )
    return ev


def _load_in_game_intervals(
    mdb,
    date_key: str,
    player: PlayerIdentity,
    start_us_utc: Optional[int] = None,
    end_us_utc: Optional[int] = None,
) -> tuple[list[dict[str, Any]], list[tuple[datetime, datetime]]]:
    """
    從 FishPlayerInOutTime_YYYYMMDD 取得玩家在「遊戲內」的區間（EnterTs/LeaveTs，microseconds）。
    同時帶出遊玩遊戲資訊（GameName/StageName）。
    """
    col_name = f"FishPlayerInOutTime_{date_key}"
    col = mdb.get_collection(col_name)

    player_filter = []
    if player.ark_id:
        player_filter.append({"ArkID": player.ark_id})
    if player.user_id:
        player_filter.append({"UserID": player.user_id})
    if player.nickname:
        player_filter.append({"NickName": player.nickname})

    if not player_filter:
        return ([], [])

    q: dict[str, Any] = {"$or": player_filter}
    if start_us_utc is not None or end_us_utc is not None:
        # 粗略用 EnterTs 做範圍過濾（足夠縮小資料量）
        r: dict[str, Any] = {}
        if start_us_utc is not None:
            r["$gte"] = int(start_us_utc)
        if end_us_utc is not None:
            r["$lte"] = int(end_us_utc)
        q["EnterTs"] = r

    cursor = col.find(
        q,
        {"_id": 0, "EnterTs": 1, "LeaveTs": 1, "GameName": 1, "StageName": 1},
    ).sort("EnterTs", pymongo.ASCENDING)

    spans: list[dict[str, Any]] = []
    intervals: list[tuple[datetime, datetime]] = []
    for doc in cursor:
        ent = doc.get("EnterTs")
        lev = doc.get("LeaveTs")
        if ent is None or lev is None:
            continue
        try:
            s = _ts_us_to_dt_utc(int(ent))
            e = _ts_us_to_dt_utc(int(lev))
        except Exception:
            continue
        if e < s:
            s, e = e, s
        game = (doc.get("GameName") or doc.get("StageName") or "").strip()
        label = game if game else "in game"
        spans.append({"start": s, "end": e, "label": label})
        intervals.append((s, e))

    # merge overlaps
    intervals.sort(key=lambda p: p[0])
    merged: list[tuple[datetime, datetime]] = []
    for s, e in intervals:
        if not merged or s > merged[-1][1]:
            merged.append((s, e))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
    spans.sort(key=lambda d: d["start"])
    return (spans, merged)


def _detect_special_events(
    series: list[dict[str, Any]],
    big_win_threshold: float,
    big_mult_threshold: float,
    special_fish_list: list[str],
) -> list[dict[str, Any]]:
    """
    SessionGame 是單局/單次事件資料：
    - 贏大分：WinAmount >= threshold
    - 大倍：(WinAmount/EffectBet) >= threshold
    - 指定魚：TempText 解析出的 fish id 命中清單就標記
    """
    ev: list[dict[str, Any]] = []

    special_set = {s.strip().upper() for s in special_fish_list if s.strip()}

    for row in series:
        t = row["t"]
        win_amount = row.get("win_amount")
        mult = row.get("mult")

        fish_ids = row.get("fish_ids") or []
        hit = [fid for fid in fish_ids if fid in special_set]
        if hit:
            ev.append({"t": t, "kind": "special_fish", "label": "special fish: " + ",".join(hit)})

        if win_amount is not None and win_amount >= big_win_threshold:
            # 優先用 raw 的 target，其次才用 TempText 解析
            raw_targets = row.get("raw_targets") or []
            target = ",".join(raw_targets) if raw_targets else (",".join(fish_ids) if fish_ids else "-")
            ev.append(
                {
                    "t": t,
                    "kind": "big_win",
                    "label": f"big win: {win_amount:.2f} (>= {big_win_threshold:.2f})<br>target={target}",
                }
            )

        if mult is not None and mult >= big_mult_threshold:
            raw_targets = row.get("raw_targets") or []
            target = ",".join(raw_targets) if raw_targets else (",".join(fish_ids) if fish_ids else "-")
            ev.append(
                {
                    "t": t,
                    "kind": "big_mult",
                    "label": f"big mult: x{mult:.2f} (>= x{big_mult_threshold:.2f})<br>target={target}",
                }
            )

    return ev


def _build_figure(
    series: list[dict[str, Any]],
    events: list[dict[str, Any]],
    title: str,
    in_game_intervals_utc: Optional[list[tuple[datetime, datetime]]] = None,
    raw_rows_utc: Optional[list[dict[str, Any]]] = None,
    in_game_spans_utc: Optional[list[dict[str, Any]]] = None,
) -> "go.Figure":
    display_tz = timezone(timedelta(hours=INPUT_TZ_OFFSET_HOURS))
    x = [r["t"].astimezone(display_tz) for r in series if r.get("asset") is not None]
    y = [r["asset"] for r in series if r.get("asset") is not None]

    # in-game 判定（用 PlayerInOut intervals，全部以 UTC 判斷）
    ig = sorted(in_game_intervals_utc or [], key=lambda p: p[0])
    ig_starts = [p[0] for p in ig]

    def is_in_game_utc(t_utc: datetime) -> bool:
        if not ig:
            return False
        # intervals 已 merge & sorted
        # 找最後一個 start <= t
        i = bisect_right(ig_starts, t_utc) - 1
        if i < 0:
            return False
        return ig[i][0] <= t_utc <= ig[i][1]

    # 圖表順序：asset, OriginalBet, Weapon, Shooting, InGame
    # row heights：weapon 與 in-game 依項目數量動態調整，且兩者每個 item 高度對齊；
    # shooting 的高度為 1/3 item。
    raw_all = sorted(raw_rows_utc or [], key=lambda r: r.get("t", datetime.min.replace(tzinfo=timezone.utc)))
    raw_g0 = [rr for rr in raw_all if isinstance(rr.get("t"), datetime) and is_in_game_utc(rr["t"])]
    weapon_items0 = sorted({str(r.get("weapon")) for r in raw_g0 if r.get("weapon") is not None})
    game_items0 = sorted({str(sp.get("label") or "in game") for sp in (in_game_spans_utc or [])})

    weapon_n = max(1, len(weapon_items0))
    game_n = max(1, len(game_items0))
    # 你希望「武器 / 遊戲中」的每個 lane 高度 = shooting 那列的高度
    # 所以把 shooting_unit 當成 lane 的單位高度
    item_unit = 1.0
    shooting_unit = item_unit / 3.0
    lane_unit = shooting_unit

    # 相對權重（Plotly 會自動 normalize）
    asset_w = 6.0
    originalbet_w = 2.0
    weapon_w = weapon_n * lane_unit
    shooting_w = shooting_unit
    ingame_w = game_n * lane_unit

    fig = make_subplots(
        rows=5,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[asset_w, originalbet_w, weapon_w, shooting_w, ingame_w],
    )

    # Row 1: asset
    # - 在遊戲裡：深色線
    # - 不在遊戲裡：淺灰色線
    # 透過 y=None 讓兩條線各自斷開
    x_all = [r["t"].astimezone(display_tz) for r in series if r.get("asset") is not None]
    y_all = [r["asset"] for r in series if r.get("asset") is not None]
    y_in = []
    y_out = []
    for r in series:
        a = r.get("asset")
        if a is None:
            continue
        if is_in_game_utc(r["t"]):
            y_in.append(a)
            y_out.append(None)
        else:
            y_in.append(None)
            y_out.append(a)

    fig.add_trace(
        go.Scatter(
            x=x_all,
            y=y_out,
            mode="lines",
            name="asset (out game)",
            line=dict(width=2, color="#cbd5e1"),
            hoverinfo="skip",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=x_all,
            y=y_in,
            mode="lines",
            name="asset (in game)",
            line=dict(width=2, color="#111827"),
            hoverinfo="skip",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=x_all,
            y=y_all,
            mode="markers",
            name="asset pts",
            marker=dict(size=6, color="#111827"),
            hovertemplate="time=%{x|%Y/%m/%d, %H:%M:%S} (UTC%{x|%z})<br>asset=%{y}<extra></extra>",
            showlegend=False,
        ),
        row=1,
        col=1,
    )

    # 為了確保第 2~5 列的 axis/domain 會被建立（shape 才會顯示），加上隱形 trace
    if x:
        for r in (2, 3, 4, 5):
            fig.add_trace(
                go.Scatter(x=[x[0], x[-1]], y=[0, 0], mode="lines", line=dict(width=0), showlegend=False, hoverinfo="skip"),
                row=r,
                col=1,
            )

    # 事件疊圖：用 bisect 找當下最近 asset（O(log n)）
    ts_utc: list[datetime] = []
    assets: list[float] = []
    for r in series:
        a = r.get("asset")
        if a is None:
            continue
        ts_utc.append(r["t"])
        assets.append(float(a))

    def asset_at(t: datetime) -> Optional[float]:
        if not ts_utc:
            return None
        j = bisect_right(ts_utc, t)
        if j <= 0:
            return assets[0]
        if j >= len(ts_utc):
            return assets[-1]

        # 線性內插，確保事件點「在線上」
        t0 = ts_utc[j - 1]
        t1 = ts_utc[j]
        y0 = assets[j - 1]
        y1 = assets[j]
        dt = (t1 - t0).total_seconds()
        if dt <= 0:
            return y0
        w = (t - t0).total_seconds() / dt
        if w < 0:
            w = 0.0
        elif w > 1:
            w = 1.0
        return y0 + (y1 - y0) * w

    style = {
        "big_win": dict(color="#d62728", symbol="star", size=12, name="big win"),
        "big_mult": dict(color="#ff7f0e", symbol="diamond", size=11, name="big mult"),
        "special_fish": dict(color="#9467bd", symbol="triangle-up", size=10, name="special fish"),
        "collect": dict(color="#2ca02c", symbol="circle-open", size=10, name="collect"),
    }

    by_kind: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        by_kind.setdefault(e["kind"], []).append(e)

    for kind, evs in by_kind.items():
        st = style.get(kind, dict(color="#7f7f7f", symbol="x", size=10, name=kind))
        ex = []
        ey = []
        text = []
        for e in evs:
            t = e["t"]
            av = asset_at(t)
            if av is None:
                continue
            ex.append(t.astimezone(display_tz))
            ey.append(av)
            text.append(e.get("label", kind))

        if not ex:
            continue

        fig.add_trace(
            go.Scatter(
                x=ex,
                y=ey,
                mode="markers",
                name=st["name"],
                marker=dict(color=st["color"], symbol=st["symbol"], size=st["size"]),
                text=text,
                hovertemplate="time=%{x|%Y/%m/%d, %H:%M:%S} (UTC%{x|%z})<br>asset=%{y}<br>%{text}<extra></extra>",
            )
        , row=1, col=1)

    # --- Rows 2-5: OriginalBet / Weapon / Shooting / InGame ---
    if len(series) > 0 or (raw_rows_utc and len(raw_rows_utc) > 0):
        # 除 asset 外，所有圖表都以 in-game 為準：不在遊戲裡就不顯示數值
        series_g = [r for r in series if is_in_game_utc(r["t"])]
        raw_g = [rr for rr in (raw_rows_utc or []) if is_in_game_utc(rr["t"])]

        last_t = None
        if raw_g:
            last_t = raw_g[-1]["t"]
        elif series_g:
            last_t = series_g[-1]["t"]
        elif series:
            last_t = series[-1]["t"]
        if last_t is None:
            return fig

        end_cap = last_t.astimezone(display_tz) + timedelta(seconds=1)

        weapon_palette = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f"]
        bet_palette = ["#e7f0fa", "#cfe1f5", "#b7d2f0", "#9fc3eb", "#87b4e6", "#6fa5e1", "#5796dc", "#3f87d7", "#2778d2"]

        def weapon_color(v: Any) -> str:
            s = (str(v) if v is not None else "-").strip()
            key = s.lower()
            # 指定顏色
            if key == "fast":
                return "#c4b5fd"  # light purple
            if key in ("lock", "redlock"):
                return "#5b21b6"  # deep purple
            if key == "tiger":
                return "#f59e0b"  # orange
            if key == "phoenix":
                return "#ef4444"  # red
            # 其他武器用穩定 hash 配色
            i = abs(hash(key))
            return weapon_palette[i % len(weapon_palette)]

        def bet_color(v: Any) -> str:
            try:
                i = int(v)
            except Exception:
                i = 0
            i = max(0, min(i, len(bet_palette) - 1))
            return bet_palette[i]

        def add_band_rect(row: int, x0: datetime, x1: datetime, color: str, opacity: float = 1.0):
            # subplot 單欄位時：row1 用 x/y，row2 用 x2/y2，依此類推
            xref = "x" if row == 1 else f"x{row}"
            yref = "y domain" if row == 1 else f"y{row} domain"
            fig.add_shape(
                type="rect",
                xref=xref,
                yref=yref,
                x0=x0,
                x1=x1,
                y0=0.0,
                y1=1.0,
                fillcolor=color,
                opacity=opacity,
                line=dict(width=0),
                layer="below",
            )

        def add_lane_rect(row: int, x0: datetime, x1: datetime, lane: int, color: str, opacity: float = 1.0):
            # weapon lane chart (one lane per weapon)
            xref = f"x{row}"
            yref = f"y{row}"
            fig.add_shape(
                type="rect",
                xref=xref,
                yref=yref,
                x0=x0,
                x1=x1,
                y0=float(lane),
                y1=float(lane + 1),
                fillcolor=color,
                opacity=opacity,
                line=dict(width=0),
                layer="below",
            )

        # weapon step bands（以單筆 FishRaw 顯示；合併連續相同狀態，減少 shape 數量）
        def compress_intervals_from_rows(rows: list[dict[str, Any]], key: str) -> list[tuple[datetime, datetime, Any]]:
            out: list[tuple[datetime, datetime, Any]] = []
            if not rows:
                return out
            cur_v = rows[0].get(key)
            cur_s = rows[0]["t"].astimezone(display_tz)
            for i in range(1, len(rows)):
                v = rows[i].get(key)
                if v != cur_v:
                    cur_e = rows[i]["t"].astimezone(display_tz)
                    if cur_e <= cur_s:
                        cur_e = cur_s + timedelta(seconds=1)
                    out.append((cur_s, cur_e, cur_v))
                    cur_v = v
                    cur_s = rows[i]["t"].astimezone(display_tz)
            out.append((cur_s, end_cap, cur_v))
            return out

        # OriginalBet: 折線圖（step line, 以單筆 FishRaw 顯示實際數值） -> row2
        bx = [r["t"].astimezone(display_tz) for r in raw_g]
        by = []
        last_b = None
        for r in raw_g:
            b = _safe_float(r.get("original_bet"))
            if b is None:
                b = last_b
            else:
                last_b = b
            by.append(b)
        fig.add_trace(
            go.Scatter(
                x=bx,
                y=by,
                mode="lines",
                name="OriginalBet",
                line=dict(color="#2563eb", width=2, shape="hv"),
                hovertemplate="time=%{x|%Y/%m/%d, %H:%M:%S} (UTC%{x|%z})<br>OriginalBet=%{y}<extra></extra>",
            ),
            row=2,
            col=1,
        )

        # Weapon: one lane per weapon (依武器值分 lane) -> row3
        weapons = sorted({str(r.get("weapon")) for r in raw_g if r.get("weapon") is not None})
        weapon_to_lane = {w: i for i, w in enumerate(weapons)}
        for s, e, v in compress_intervals_from_rows(raw_g, "weapon"):
            w = str(v) if v is not None else "-"
            lane = weapon_to_lane.get(w, len(weapon_to_lane))
            add_lane_rect(3, s, e, lane, weapon_color(v), opacity=0.95)

        # Row4 shooting: any raw point => [t, t+3s], merge overlaps（再與 in-game 取交集）
        play_windows: list[tuple[datetime, datetime]] = []
        for r in raw_g:
            s = r["t"].astimezone(display_tz)
            play_windows.append((s, s + timedelta(seconds=3)))

        play_windows.sort(key=lambda p: p[0])
        merged: list[tuple[datetime, datetime]] = []
        for s, e in play_windows:
            if not merged or s > merged[-1][1]:
                merged.append((s, e))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))

        for s, e in merged:
            # 若有 in-game 區間，先做交集，避免在遊戲外被算成 playing
            if in_game_intervals_utc:
                for gs_utc, ge_utc in in_game_intervals_utc:
                    gs = gs_utc.astimezone(display_tz)
                    ge = ge_utc.astimezone(display_tz)
                    is0 = max(s, gs)
                    ie0 = min(e, ge)
                    if ie0 > is0:
                        add_band_rect(4, is0, ie0, "#16a34a", opacity=0.65)  # deep green
            else:
                add_band_rect(4, s, e, "#16a34a", opacity=0.65)  # deep green

        # Row5 in-game: 用 y 軸 lane 顯示遊玩的遊戲
        if in_game_spans_utc:
            games = sorted({str(sp.get("label") or "in game") for sp in in_game_spans_utc})
            game_to_lane = {g: i for i, g in enumerate(games)}

            for sp in in_game_spans_utc:
                s_utc = sp.get("start")
                e_utc = sp.get("end")
                label = str(sp.get("label") or "in game")
                if not isinstance(s_utc, datetime) or not isinstance(e_utc, datetime):
                    continue
                s = s_utc.astimezone(display_tz)
                e = e_utc.astimezone(display_tz)
                if e <= s:
                    continue
                lane = game_to_lane.get(label, 0)
                add_lane_rect(5, s, e, lane, "#86efac", opacity=0.55)  # light green
        elif in_game_intervals_utc:
            for s_utc, e_utc in in_game_intervals_utc:
                s = s_utc.astimezone(display_tz)
                e = e_utc.astimezone(display_tz)
                if e <= s:
                    continue
                add_band_rect(5, s, e, "#86efac", opacity=0.45)  # light green

        # row3 weapon lanes: show ticks as weapon ids
        if weapons:
            tickvals = [i + 0.5 for i in range(len(weapons))]
            fig.update_yaxes(
                row=3,
                col=1,
                range=[0, len(weapons)],
                tickmode="array",
                tickvals=tickvals,
                ticktext=weapons,
            )
        else:
            fig.update_yaxes(showticklabels=False, ticks="", row=3, col=1)

        # row2 OriginalBet: show numeric axis
        fig.update_yaxes(row=2, col=1, showticklabels=True)

        # row4 shooting: 不需要顯示 y 軸（避免看起來像有兩條 y 軸）
        fig.update_yaxes(visible=False, row=4, col=1)
        # row5 in-game: 若有 spans 就顯示遊戲名稱 ticks，否則不顯示
        if in_game_spans_utc:
            games = sorted({str(sp.get("label") or "in game") for sp in in_game_spans_utc})
            tickvals = [i + 0.5 for i in range(len(games))]
            fig.update_yaxes(
                row=5,
                col=1,
                range=[0, len(games) if games else 1],
                tickmode="array",
                tickvals=tickvals,
                ticktext=games,
                showticklabels=True,
            )
            # 武器與遊戲中：y 軸 label 寬度依項目數動態調整（保持對齊）
            # 以最長 label 估算需要的 margin-left，讓兩個 subplot 的 plot area 對齊
            max_len = 0
            if weapons:
                max_len = max(max_len, max(len(str(w)) for w in weapons))
            if games:
                max_len = max(max_len, max(len(str(g)) for g in games))
            est_px = min(360, max(80, int(max_len * 7.2)))  # 7.2px/char 粗估
            # 只增不減，避免與外部 margin 設定打架
            cur_l = getattr(fig.layout.margin, "l", 40) if getattr(fig.layout, "margin", None) else 40
            if cur_l is None:
                cur_l = 40
            fig.update_layout(margin=dict(l=max(int(cur_l), est_px), r=20, t=60, b=40))
        else:
            fig.update_yaxes(showticklabels=False, ticks="", row=5, col=1)
        # y 軸名稱不顯示（改用每個 panel 上方標題）
        fig.update_yaxes(title_text="", row=1, col=1)
        fig.update_yaxes(title_text="", row=2, col=1)
        fig.update_yaxes(title_text="", row=3, col=1)
        fig.update_yaxes(title_text="", row=4, col=1)
        fig.update_yaxes(title_text="", row=5, col=1)

        def panel_domain_top(row: int) -> float:
            ya = fig.layout["yaxis" if row == 1 else f"yaxis{row}"]
            dom = getattr(ya, "domain", None) or [0.0, 1.0]
            return float(dom[1])

        # 每個圖表標題（顯示在 panel 上方左側）
        panel_titles = {
            1: "資產變化",
            2: "單筆押注",
            3: "武器",
            4: "打魚行為",
            5: "遊戲中",
        }
        for r, text in panel_titles.items():
            fig.add_annotation(
                x=0.0,
                y=panel_domain_top(r),
                xref="paper",
                yref="paper",
                text=f"<b>{text}</b>",
                showarrow=False,
                xanchor="left",
                yanchor="bottom",
                font=dict(size=12, color="#111827"),
            )

        # 武器/打魚行為/遊戲中：不要橫向格線（y 軸 grid）
        fig.update_yaxes(showgrid=False, row=3, col=1)
        fig.update_yaxes(showgrid=False, row=4, col=1)
        fig.update_yaxes(showgrid=False, row=5, col=1)

    fig.update_layout(
        title=title,
        xaxis_title=f"time (UTC+{INPUT_TZ_OFFSET_HOURS})",
        height=650,
        legend=dict(orientation="h"),
        margin=dict(l=40, r=20, t=60, b=40),
    )
    fig.update_xaxes(tickformat="%Y/%m/%d, %H:%M:%S")
    return fig


TEMPLATE_NAME = "FishTeeYanAnalyze.html"


def _stats_row_key(game: str, ark_id: str, user_id: str) -> str:
    """Stable GET-safe key for ignore checkboxes (no raw '|' in parts)."""
    g = quote(str(game or ""), safe="")
    a = quote(str(ark_id or ""), safe="")
    u = quote(str(user_id or ""), safe="")
    return f"{g}::{a}::{u}"


def _load_inout_spans_by_ark_us(
    mdb,
    date_key: str,
    start_us: int,
    end_us: int,
) -> dict[str, list[tuple[int, int, str]]]:
    """
    讀 FishPlayerInOutTime，回傳每個 ArkID 的 (EnterTs, LeaveTs, game_label) 列表（microseconds）。
    只取與查詢時間窗有重疊的紀錄。
    """
    col = mdb.get_collection(f"FishPlayerInOutTime_{date_key}")
    q = {"$and": [{"EnterTs": {"$lte": int(end_us)}}, {"LeaveTs": {"$gte": int(start_us)}}]}
    cursor = col.find(
        q,
        {"_id": 0, "ArkID": 1, "EnterTs": 1, "LeaveTs": 1, "GameName": 1, "StageName": 1},
    ).sort("EnterTs", pymongo.ASCENDING)

    out: dict[str, list[tuple[int, int, str]]] = {}
    for doc in cursor:
        ark = doc.get("ArkID")
        if ark is None:
            continue
        ent = doc.get("EnterTs")
        lev = doc.get("LeaveTs")
        if ent is None or lev is None:
            continue
        try:
            s = int(ent)
            e = int(lev)
        except Exception:
            continue
        if e < s:
            s, e = e, s
        label = (doc.get("GameName") or doc.get("StageName") or "").strip() or "in game"
        ark_s = str(ark)
        out.setdefault(ark_s, []).append((s, e, label))
    for ark_s, spans in out.items():
        spans.sort(key=lambda t: t[0])
    return out


def _game_from_inout(spans: list[tuple[int, int, str]], ts_us: int) -> Optional[str]:
    """若 ts 落在任一 in-out 區間，回傳該區間 label（先命中先回傳）。"""
    for s, e, lab in spans:
        if s <= ts_us <= e:
            return lab
    return None


def _aggregate_fish_stats_by_game_player(
    mdb,
    date_key: str,
    start_us: int,
    end_us: int,
    game_substring: str = "",
) -> list[dict[str, Any]]:
    """
    以 DetailBetWinFishRaw 彙總：遊戲 × 玩家。
    遊戲名優先取文件 GameName/StageName；否則用 FishPlayerInOutTime 對 CreateTs 對應。
    """
    col = mdb.get_collection(f"DetailBetWinFishRaw_{date_key}")
    inout_by_ark = _load_inout_spans_by_ark_us(mdb, date_key, start_us, end_us)

    filt = (game_substring or "").strip().lower()

    # key: (game_label, ark_id, user_id)
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}

    q: dict[str, Any] = {"CreateTs": {"$gte": int(start_us), "$lte": int(end_us)}}
    cursor = col.find(
        q,
        {
            "_id": 0,
            "ArkID": 1,
            "UserID": 1,
            "NickName": 1,
            "CreateTs": 1,
            "GameName": 1,
            "StageName": 1,
            "BetAmount": 1,
            "WinAmount": 1,
            "OriginalBet": 1,
            "BetCount": 1,
            "WinCount": 1,
        },
        batch_size=500,
    )

    for doc in cursor:
        ts = doc.get("CreateTs")
        if ts is None:
            continue
        try:
            ts_i = int(ts)
        except Exception:
            continue

        ark = doc.get("ArkID")
        ark_s = str(ark) if ark is not None else ""
        uid = doc.get("UserID")
        uid_s = str(uid) if uid is not None else ""
        nick = doc.get("NickName")
        nick_s = str(nick) if nick is not None else ""

        gdoc = (str(doc.get("GameName") or "").strip() or str(doc.get("StageName") or "").strip())
        if gdoc:
            game_label = gdoc
        else:
            spans = inout_by_ark.get(ark_s, [])
            game_label = _game_from_inout(spans, ts_i) or "(未對應進出表)"

        if filt and filt not in game_label.lower():
            continue

        ba = _safe_float(doc.get("BetAmount")) or 0.0
        wa = _safe_float(doc.get("WinAmount")) or 0.0
        ob = _safe_float(doc.get("OriginalBet"))
        try:
            bc = int(doc.get("BetCount") or 0)
        except Exception:
            bc = 0
        try:
            wc = int(doc.get("WinCount") or 0)
        except Exception:
            wc = 0

        key = (game_label, ark_s, uid_s)
        agg = buckets.setdefault(
            key,
            {
                "game": game_label,
                "ark_id": ark_s,
                "user_id": uid_s,
                "nicknames": set(),
                "total_bet": 0.0,
                "total_win": 0.0,
                "total_original_bet": 0.0,  # sum(OriginalBet * BetCount)
                "bet_count": 0,
                "win_count": 0,
                "row_count": 0,
            },
        )
        if nick_s:
            agg["nicknames"].add(nick_s)
        agg["total_bet"] += float(ba)
        agg["total_win"] += float(wa)
        if ob is not None and bc > 0:
            agg["total_original_bet"] += float(ob) * float(bc)
        agg["bet_count"] += bc
        agg["win_count"] += wc
        agg["row_count"] += 1

    rows: list[dict[str, Any]] = []
    for (_g, ark_s, uid_s), agg in buckets.items():
        tb = float(agg["total_bet"])
        tw = float(agg["total_win"])
        net = tw - tb
        rtp = (tw / tb) if tb > 0 else None
        rtp_pct = (rtp * 100.0) if rtp is not None else None
        tob = float(agg["total_original_bet"] or 0.0)
        srtp = (tw / tob) if tob > 0 else None
        srtp_pct = (srtp * 100.0) if srtp is not None else None
        nick_display = ", ".join(sorted(agg["nicknames"])) if agg["nicknames"] else "-"
        game = str(agg["game"])
        row_key = _stats_row_key(game, ark_s, uid_s)
        rows.append(
            {
                "row_key": row_key,
                "game": game,
                "ark_id": ark_s or "-",
                "user_id": uid_s or "-",
                "nickname": nick_display,
                "total_bet": tb,
                "total_win": tw,
                "net": net,
                "total_original_bet": tob,
                "bet_count": int(agg["bet_count"]),
                "win_count": int(agg["win_count"]),
                "row_count": int(agg["row_count"]),
                "rtp": rtp,
                "rtp_pct": rtp_pct,
                "srtp": srtp,
                "srtp_pct": srtp_pct,
                "win_rate": (int(agg["win_count"]) / int(agg["bet_count"])) if int(agg["bet_count"]) > 0 else None,
                "hit_pct": (float(agg["win_count"]) / float(agg["bet_count"]) * 100.0)
                if int(agg["bet_count"]) > 0
                else None,
            }
        )

    rows.sort(key=lambda r: (r["game"].lower(), -r["total_bet"], r["ark_id"]))
    return rows


def _stats_overall(rows: list[dict[str, Any]], ignored_keys: set[str]) -> dict[str, Any]:
    tb = tw = 0.0
    tob = 0.0
    bc = wc = rc = 0
    for r in rows:
        if r.get("row_key") in ignored_keys:
            continue
        tb += float(r.get("total_bet") or 0.0)
        tw += float(r.get("total_win") or 0.0)
        tob += float(r.get("total_original_bet") or 0.0)
        bc += int(r.get("bet_count") or 0)
        wc += int(r.get("win_count") or 0)
        rc += int(r.get("row_count") or 0)
    net = tw - tb
    rtp = (tw / tb) if tb > 0 else None
    srtp = (tw / tob) if tob > 0 else None
    win_rate = (wc / bc) if bc > 0 else None
    return {
        "total_bet": tb,
        "total_win": tw,
        "net": net,
        "bet_count": bc,
        "win_count": wc,
        "row_count": rc,
        "rtp": rtp,
        "rtp_pct": (rtp * 100.0) if rtp is not None else None,
        "srtp": srtp,
        "srtp_pct": (srtp * 100.0) if srtp is not None else None,
        "win_rate": win_rate,
        "hit_pct": (win_rate * 100.0) if win_rate is not None else None,
        "included_rows": sum(1 for r in rows if r.get("row_key") not in ignored_keys),
    }


def _handle_play_fish_analyze():
    db_env = (request.args.get("db_env", "macross-test") or "macross-test").strip()
    preset = DB_PRESETS.get(db_env) or DB_PRESETS["macross-test"]
    mongo_uri = preset["mongo_uri"]
    mongo_db = preset["mongo_db"]
    date = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    player = request.args.get("player", "")
    start_time = request.args.get("start_time", "00:00")
    end_time = request.args.get("end_time", "23:59")
    active_tab = request.args.get("tab", "playtime")
    defer = request.args.get("defer", "0") in ("1", "true", "True", "yes", "Y")

    asset_mode = DEFAULT_ASSET_MODE
    big_win = request.args.get("big_win", str(DEFAULT_BIG_WIN_THRESHOLD))
    big_mult = request.args.get("big_mult", str(DEFAULT_BIG_MULT_THRESHOLD))
    special_fish = request.args.get("special_fish", ",".join(DEFAULT_SPECIAL_FISH_LIST))
    refresh = request.args.get("refresh", "0") in ("1", "true", "True", "yes", "Y")

    warning = ""
    charts = []
    meta = ""
    inout_rows = []

    # 分頁切換保留上次資訊（排除 tab/auto/defer/忽略勾選）
    qs_pairs: list[tuple[str, str]] = []
    for k in request.args:
        if k in ("tab", "auto", "defer", "stat_ignore", "stats_run"):
            continue
        for v in request.args.getlist(k):
            qs_pairs.append((k, v))
    qs = urlencode(qs_pairs, doseq=True)

    stats_game = (request.args.get("stats_game") or "").strip()
    stat_ignore_keys = {x for x in request.args.getlist("stat_ignore") if x.strip()}
    stats_rows: list[dict[str, Any]] = []
    stats_overall: Optional[dict[str, Any]] = None
    stats_ran = request.args.get("stats_run", "") in ("1", "true", "True", "yes", "Y")

    # 遊玩時間查詢：跨日搜尋 InOutTime，提供套用到詳細分析的連結
    if active_tab == "playtime":
        if player.strip():
            try:
                mdb = _MONGO_DBS.get(db_env)
                if mdb is None:
                    # fallback：若初始化失敗/尚未初始化，才懶載入
                    mdb = _build_mongo(mongo_uri, mongo_db)
                # 往回搜尋近 N 天
                today_local = datetime.now(timezone(timedelta(hours=INPUT_TZ_OFFSET_HOURS))).date()
                q_player = player.strip()
                for d_off in range(0, max(1, PLAYTIME_SEARCH_DAYS)):
                    d_local = today_local - timedelta(days=d_off)
                    date_str = d_local.strftime("%Y-%m-%d")
                    date_key = _to_date_key(date_str)

                    # 直接在 daily collection 內用 player query 找紀錄（跨 ArkID）
                    col_name = f"FishPlayerInOutTime_{date_key}"
                    col = mdb.get_collection(col_name)
                    cursor = col.find(
                        {
                            "$or": [
                                {"ArkID": q_player},
                                {"UserID": q_player},
                                {"NickName": q_player},
                                {"MerchantUserAccount": q_player},
                            ]
                        },
                        {"_id": 0, "ArkID": 1, "EnterTs": 1, "LeaveTs": 1, "GameName": 1, "StageName": 1},
                    ).sort("EnterTs", pymongo.ASCENDING)

                    for doc in cursor:
                        ent = doc.get("EnterTs")
                        lev = doc.get("LeaveTs")
                        if ent is None or lev is None:
                            continue
                        s_utc = _ts_us_to_dt_utc(int(ent))
                        e_utc = _ts_us_to_dt_utc(int(lev))
                        if e_utc < s_utc:
                            s_utc, e_utc = e_utc, s_utc

                        s_local = _dt_utc_to_local(s_utc)
                        e_local = _dt_utc_to_local(e_utc)
                        s_round = _round_down_to_10min(s_local)
                        e_round = _round_up_to_10min(e_local)

                        label = (doc.get("GameName") or doc.get("StageName") or "").strip() or "in game"
                        ark_id = str(doc.get("ArkID") or "-")
                        inout_rows.append(
                            {
                                "ark_id": ark_id,
                                "label": label,
                                "start_local": s_local.strftime("%Y/%m/%d, %H:%M:%S"),
                                "end_local": e_local.strftime("%Y/%m/%d, %H:%M:%S"),
                                "start_rounded": _dt_local_to_hhmm(s_round),
                                "end_rounded": _dt_local_to_hhmm(e_round),
                                "date": date_str,
                                "player_for_analysis": ark_id if ark_id != "-" else q_player,
                            }
                        )

                inout_rows.sort(key=lambda r: (r["date"], r["ark_id"], r["start_local"]))
            except Exception as e:
                print("=== FishTeeYanAnalyzeWeb error ===")
                print(traceback.format_exc())
                warning = f"InOut 查詢失敗：{type(e).__name__}: {e}"

        return render_template(
            TEMPLATE_NAME,
            brand_title="Fish 玩家遊玩狀況分析",
            brand_url="/PlayFishAnalyze",
            active_route="play_fish_analyze",
            show_env_select=True,
            active_tab="playtime",
            qs=qs,
            date=date,
            player=player,
            start_time=start_time,
            end_time=end_time,
            big_win=request.args.get("big_win", str(DEFAULT_BIG_WIN_THRESHOLD)),
            big_mult=request.args.get("big_mult", str(DEFAULT_BIG_MULT_THRESHOLD)),
            special_fish=request.args.get("special_fish", ",".join(DEFAULT_SPECIAL_FISH_LIST)),
            warning=warning,
            charts=[],
            meta="",
            inout_rows=inout_rows,
            playtime_days=PLAYTIME_SEARCH_DAYS,
            db_env=db_env,
            stats_rows=[],
            stats_overall=None,
            stats_game=stats_game,
            stat_ignore_keys=stat_ignore_keys,
            stats_ran=False,
        )

    if active_tab == "stats":
        if stats_ran:
            try:
                date_key = _to_date_key(date)
            except Exception:
                warning = "日期格式錯誤，請用 YYYY-MM-DD"
                stats_rows = []
                stats_overall = _stats_overall([], set())
            else:
                try:
                    start_dt, end_dt = _build_time_range_utc(date, start_time, end_time)
                    start_us = int(start_dt.timestamp() * 1_000_000)
                    end_us = int(end_dt.timestamp() * 1_000_000)
                    mdb = _MONGO_DBS.get(db_env)
                    if mdb is None:
                        mdb = _build_mongo(mongo_uri, mongo_db)
                    stats_rows = _aggregate_fish_stats_by_game_player(
                        mdb, date_key, start_us, end_us, game_substring=stats_game
                    )
                    stats_overall = _stats_overall(stats_rows, stat_ignore_keys)
                    meta = (
                        f"統計 | DetailBetWinFishRaw_{date_key} | UTC "
                        f"{start_dt.strftime('%H:%M')}-{end_dt.strftime('%H:%M')} | "
                        f"groups={len(stats_rows)} ignored={len(stat_ignore_keys)}"
                    )
                except Exception as e:
                    print("=== FishTeeYanAnalyzeWeb stats error ===")
                    print(traceback.format_exc())
                    warning = f"統計查詢失敗：{type(e).__name__}: {e}"
                    stats_rows = []
                    stats_overall = _stats_overall([], set())
        else:
            stats_overall = None

        return render_template(
            TEMPLATE_NAME,
            brand_title="Fish 玩家遊玩狀況分析",
            brand_url="/PlayFishAnalyze",
            active_route="play_fish_analyze",
            show_env_select=True,
            active_tab="stats",
            qs=qs,
            date=date,
            player=player,
            start_time=start_time,
            end_time=end_time,
            big_win=big_win,
            big_mult=big_mult,
            special_fish=special_fish,
            warning=warning,
            charts=[],
            meta=meta,
            inout_rows=[],
            playtime_days=PLAYTIME_SEARCH_DAYS,
            db_env=db_env,
            stats_rows=stats_rows,
            stats_overall=stats_overall,
            stats_game=stats_game,
            stat_ignore_keys=stat_ignore_keys,
            stats_ran=stats_ran,
        )

    # 只有在使用者真的按了參數（至少 player 有填）才跑分析查詢，避免一開頁就打 DB
    if (not defer) and player.strip():
        # 查詢快取 key（同條件 5 分鐘內秒回）
        cache_key = (
            db_env,
            date,
            player.strip(),
            start_time,
            end_time,
            str(big_win),
            str(big_mult),
            (special_fish or "").strip(),
        )
        if not refresh:
            cached = _cache_get(cache_key)
            if cached:
                warning = cached.get("warning", "")
                charts = cached.get("charts", [])
                meta = (cached.get("meta", "") or "") + " | cache=HIT"
                return render_template(
                    TEMPLATE_NAME,
                    brand_title="Fish 玩家遊玩狀況分析",
                    brand_url="/PlayFishAnalyze",
                    active_route="play_fish_analyze",
                    show_env_select=True,
                    active_tab="detail",
                    qs=qs,
                    date=date,
                    player=player,
                    start_time=start_time,
                    end_time=end_time,
                    big_win=big_win,
                    big_mult=big_mult,
                    special_fish=special_fish,
                    warning=warning,
                    charts=charts,
                    meta=meta,
                    inout_rows=[],
                    playtime_days=PLAYTIME_SEARCH_DAYS,
                    db_env=db_env,
                    stats_rows=[],
                    stats_overall=None,
                    stats_game=stats_game,
                    stat_ignore_keys=stat_ignore_keys,
                    stats_ran=False,
                )

        try:
            date_key = _to_date_key(date)
        except Exception:
            warning = "日期格式錯誤，請用 YYYY-MM-DD"
            return render_template(
                TEMPLATE_NAME,
                brand_title="Fish 玩家遊玩狀況分析",
                brand_url="/PlayFishAnalyze",
                active_route="play_fish_analyze",
                show_env_select=True,
                active_tab="detail",
                qs=qs,
                date=date,
                player=player,
                start_time=start_time,
                end_time=end_time,
                big_win=big_win,
                big_mult=big_mult,
                special_fish=special_fish,
                warning=warning,
                charts=[],
                meta="",
                inout_rows=[],
                playtime_days=PLAYTIME_SEARCH_DAYS,
                db_env=db_env,
                stats_rows=[],
                stats_overall=None,
                stats_game=stats_game,
                stat_ignore_keys=stat_ignore_keys,
                stats_ran=False,
            )

        try:
            start_dt, end_dt = _build_time_range_utc(date, start_time, end_time)
            mdb = _MONGO_DBS.get(db_env)
            if mdb is None:
                # fallback：若初始化失敗/尚未初始化，才懶載入
                mdb = _build_mongo(mongo_uri, mongo_db)
            players = _resolve_players(mdb, date_key, player.strip())

            bw = float(big_win)
            bm = float(big_mult)
            sf = [s.strip() for s in special_fish.split(",") if s.strip()]

            total_points = 0
            total_events = 0
            resolved = []

            for pid in players:
                resolved.append(f"ark_id={pid.ark_id} user_id={pid.user_id} nickname={pid.nickname}")
                series = _load_detail_betwin_fish_raw_series(
                    mdb,
                    date_key,
                    pid,
                    start_us_utc=int(start_dt.timestamp() * 1_000_000),
                    end_us_utc=int(end_dt.timestamp() * 1_000_000),
                )
                if not series:
                    continue

                # 這裡 raw_rows 用同一份序列來源（raw），供 OriginalBet/Weapon/Shooting 與事件顯示
                # 取出序列中的 wagers_id 後再抓完整 raw_rows（可含 bet_count 等欄位）
                wid_list = sorted({int(r["wagers_id"]) for r in series if r.get("wagers_id") is not None})
                raw_rows = _load_detail_betwin_fish_raw_rows(
                    mdb,
                    date_key,
                    pid,
                    wid_list,
                    start_us_utc=int(start_dt.timestamp() * 1_000_000),
                    end_us_utc=int(end_dt.timestamp() * 1_000_000),
                )

                in_game_spans, in_game_intervals = _load_in_game_intervals(
                    mdb,
                    date_key,
                    pid,
                    start_us_utc=int(start_dt.timestamp() * 1_000_000),
                    end_us_utc=int(end_dt.timestamp() * 1_000_000),
                )

                # 資產變化：每次 in-game 起點歸 0
                series = _rebase_asset_series_by_in_game(series, in_game_intervals)

                events = []
                # 大贏/大倍/指定魚事件改用單筆 FishRaw 計算（不聚合）
                events.extend(_raw_rows_to_events(raw_rows, big_win_threshold=bw, big_mult_threshold=bm, special_fish_list=sf))
                events.extend(_load_collect_events(mdb, date_key, pid, start_s_utc=int(start_dt.timestamp()), end_s_utc=int(end_dt.timestamp())))

                title = f"{date} | ark_id={pid.ark_id or '-'} | nick={pid.nickname or '-'}"
                summary_html = _compute_ratios_html(raw_rows, in_game_intervals, start_dt, end_dt)
                fig = _build_figure(
                    series,
                    events,
                    title,
                    in_game_intervals_utc=in_game_intervals,
                    in_game_spans_utc=in_game_spans,
                    raw_rows_utc=raw_rows,
                )
                charts.append(
                    {
                        "title": title,
                        "summary": summary_html,
                        "div": fig.to_html(full_html=False, include_plotlyjs="cdn"),
                    }
                )
                total_points += len(series)
                total_events += len(events)

            meta = "resolved players: " + " ; ".join(resolved)
            if not charts:
                warning = (
                    "查不到 DetailBetWinFishRaw 資料。"
                    "如果你是用 NickName 查不到，請改用 ark_id(ArkID) 或 UserID；或確認當天 collection 是否存在。"
                )
            else:
                meta = meta + f" | charts={len(charts)} points={total_points} events={total_events}"
        except Exception as e:
            # 讓 terminal 一定看得到完整堆疊，方便定位像 "Database object is not callable" 這類錯誤來源
            print("=== FishTeeYanAnalyzeWeb error ===")
            print(traceback.format_exc())
            warning = f"查詢/繪圖失敗：{type(e).__name__}: {e}"

        # 寫回快取（包含查不到資料/警告）
        _cache_set(
            cache_key,
            {
                "warning": warning,
                "charts": charts,
                "meta": meta,
            },
        )

    return render_template(
        TEMPLATE_NAME,
        brand_title="Fish 玩家遊玩狀況分析",
        brand_url="/PlayFishAnalyze",
        active_route="play_fish_analyze",
        show_env_select=True,
        active_tab="detail",
        qs=qs,
        date=date,
        player=player,
        start_time=start_time,
        end_time=end_time,
        big_win=big_win,
        big_mult=big_mult,
        special_fish=special_fish,
        warning=warning,
        charts=charts,
        meta=meta + (" | cache=BYPASS" if refresh else " | cache=MISS"),
        inout_rows=[],
        playtime_days=PLAYTIME_SEARCH_DAYS,
        db_env=db_env,
        stats_rows=[],
        stats_overall=None,
        stats_game=stats_game,
        stat_ignore_keys=stat_ignore_keys,
        stats_ran=False,
    )


def init_play_fish_analyze_routes(flask_app: Flask) -> None:
    """
    Register Fish analyze page routes onto an existing Flask app.

    - GET /PlayFishAnalyze
    - GET /PlayFishAnalyze/ (redirect-safe alias)
    """
    global _MONGO_DBS  # noqa: PLW0603
    if not _MONGO_DBS:
        # 服務啟動時就建立兩個 MongoDB handle（符合你要的「運作起來就都準備好」）
        for env, preset in DB_PRESETS.items():
            try:
                _MONGO_DBS[env] = _build_mongo(preset["mongo_uri"], preset["mongo_db"])
            except Exception as e:
                print(f"[FishTeeYanAnalyzeWeb] Mongo init failed for {env}: {type(e).__name__}: {e}")

    @flask_app.route("/PlayFishAnalyze", methods=["GET"], endpoint="play_fish_analyze")
    def play_fish_analyze():  # noqa: F811
        return _handle_play_fish_analyze()

    @flask_app.route("/PlayFishAnalyze/", methods=["GET"], endpoint="play_fish_analyze_slash")
    def play_fish_analyze_slash():  # noqa: F811
        return _handle_play_fish_analyze()


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "1") not in ("0", "false", "False")
    init_play_fish_analyze_routes(app)
    # Windows 上 reloader 有時會出現 WinError 10038（socket 不是通訊端）導致舊版本混跑
    app.run(host=WEB_HOST, port=WEB_PORT, debug=debug, use_reloader=False)