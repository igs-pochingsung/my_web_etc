from __future__ import annotations

import io
import os
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from flask import Flask, request, render_template, send_file

try:
    import plotly.graph_objects as go
except Exception as e:  # pragma: no cover
    raise SystemExit(
        "需要安裝 plotly。請先 `py -3 -m pip install plotly`。\n"
        f"import error: {e}"
    )

import pymongo

from DBConnect import get_db_conn, list_envs


TEMPLATE_NAME = "ExperienceMeetingAnalyze.html"

INPUT_TZ_OFFSET_HOURS = int(os.getenv("INPUT_TZ_OFFSET_HOURS", "8"))

_MACROSS = get_db_conn("ExperienceMeetingAnalyzeWeb", "macross-test")
DEFAULT_MONGO_URI = str(_MACROSS.get("mongo_uri", "")).strip()
DEFAULT_MONGO_DB = str(_MACROSS.get("mongo_db", "Data")).strip()
DEFAULT_PINUSER_COL = os.getenv("EXPERIENCE_PINUSER_COL", "PinUser").strip()
DEFAULT_PURCHASE_COL = os.getenv("EXPERIENCE_PURCHASE_COL", "PurchaseHistory").strip()
DEFAULT_LOG_COL = os.getenv("EXPERIENCE_LOG_COL", "SessionGame_20250110").strip()
DEFAULT_THEME_TITLE = os.getenv("EXPERIENCE_THEME_TITLE", "SkyKing").strip()

# 與 PlayFishAnalyze 對齊：提供環境選擇（db_env）
_SSSAPI = get_db_conn("ExperienceMeetingAnalyzeWeb", "sssapi-test")
SSSAPI_MONGO_URI = str(_SSSAPI.get("mongo_uri", DEFAULT_MONGO_URI)).strip()
# gd-test：SSH tunnel 本機綁定埠 → 遠端 Mongo 27017（遠端埠寫在 tunnel 設定裡，URI 只寫本機埠）
# 例：remote sweepstakes-...:27017 → local BindPort 17122 → PyMongo 用 mongodb://127.0.0.1:17122
_GD = get_db_conn("ExperienceMeetingAnalyzeWeb", "gd-test")
GD_TEST_MONGO_URI = str(_GD.get("mongo_uri", "")).strip()
GD_TEST_MONGO_DB = str(_GD.get("mongo_db", DEFAULT_MONGO_DB)).strip()

DB_PRESETS: dict[str, dict[str, str]] = {
    "macross-test": {"mongo_uri": DEFAULT_MONGO_URI, "mongo_db": DEFAULT_MONGO_DB},
    "sssapi-test": {"mongo_uri": SSSAPI_MONGO_URI, "mongo_db": DEFAULT_MONGO_DB},
    "gd-test": {"mongo_uri": GD_TEST_MONGO_URI, "mongo_db": GD_TEST_MONGO_DB},
}
_MONGO_DBS: dict[str, Any] = {}

# 5 分鐘查詢快取（避免同參數重算）
CACHE_TTL_SEC = int(os.getenv("EXPERIENCE_CACHE_TTL_SEC", "300"))
_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}


@dataclass(frozen=True)
class _Option:
    value: str
    label: str


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        if isinstance(v, str):
            s = v.strip().replace(",", "")
            if s == "":
                return None
            return float(s)
        return float(v)
    except Exception:
        return None


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


def _parse_pins(pins_raw: str) -> list[int]:
    """
    pins_raw 支援：
    - 逗號/空白/換行分隔
    - 範圍 1000-1010
    """
    s = (pins_raw or "").strip()
    if not s:
        return []
    parts = []
    for tok in s.replace("\r", " ").replace("\n", " ").replace(",", " ").split():
        tok = tok.strip()
        if not tok:
            continue
        parts.append(tok)

    out: list[int] = []
    for p in parts:
        if "-" in p and p.count("-") == 1:
            a, b = p.split("-", 1)
            try:
                ia = int(a.strip())
                ib = int(b.strip())
            except Exception:
                continue
            if ib < ia:
                ia, ib = ib, ia
            out.extend(list(range(ia, ib + 1)))
            continue
        try:
            out.append(int(p))
        except Exception:
            continue

    # de-dup preserve order
    seen = set()
    uniq: list[int] = []
    for x in out:
        if x in seen:
            continue
        seen.add(x)
        uniq.append(x)
    return uniq


def _build_mongo(uri: str, db_name: str):
    client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=15000, connectTimeoutMS=10000)
    # ping early for clear error
    client.admin.command("ping")
    return client[db_name]


def _get_mdb(db_env: str):
    env = (db_env or "macross-test").strip() or "macross-test"
    preset = DB_PRESETS.get(env) or DB_PRESETS["macross-test"]
    cached = _MONGO_DBS.get(env)
    if cached is not None:
        return env, preset, cached
    mdb = _build_mongo(preset["mongo_uri"], preset["mongo_db"])
    _MONGO_DBS[env] = mdb
    return env, preset, mdb


def _parse_dt_local(dt_str: str) -> Optional[datetime]:
    """Parse datetime-local (YYYY-MM-DDTHH:MM) as local time then convert to UTC."""
    s = (dt_str or "").strip()
    if not s:
        return None
    try:
        local = datetime.strptime(s, "%Y-%m-%dT%H:%M").replace(
            tzinfo=timezone(timedelta(hours=INPUT_TZ_OFFSET_HOURS))
        )
        return local.astimezone(timezone.utc)
    except Exception:
        return None


@dataclass(frozen=True)
class _TimeField:
    field: str
    unit: str  # 's' | 'ms' | 'us'


def _detect_time_field(col, base_query: dict[str, Any]) -> Optional[_TimeField]:
    """
    Guess time field & unit by sampling one doc.
    """
    doc = col.find_one(base_query, {"_id": 0})
    if not doc:
        return None
    candidates = ["CreateTimeTS", "CreateTs", "CreateTime", "CreateTimeMs", "CreateTimeMS", "CreateTS"]
    for f in candidates:
        if f not in doc:
            continue
        v = doc.get(f)
        if v is None:
            continue
        try:
            n = int(v)
        except Exception:
            continue

        if f in ("CreateTimeTS", "CreateTime", "CreateTS"):
            return _TimeField(field=f, unit="s")
        if f.lower().endswith("ms"):
            return _TimeField(field=f, unit="ms")
        # CreateTs: infer by magnitude
        if n >= 10**14:
            return _TimeField(field=f, unit="us")
        if n >= 10**11:
            return _TimeField(field=f, unit="ms")
        return _TimeField(field=f, unit="s")
    return None


def _to_range_value(dt_utc: datetime, unit: str) -> int:
    ts = dt_utc.timestamp()
    if unit == "us":
        return int(ts * 1_000_000)
    if unit == "ms":
        return int(ts * 1_000)
    return int(ts)


def _compute_experience_stats(
    mdb,
    pinuser_col: str,
    purchase_col: str,
    log_col: str,
    theme_title: str,
    pins: list[int],
    start_dt_utc: Optional[datetime] = None,
    end_dt_utc: Optional[datetime] = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """
    依 `DataStatistics.startStatistic()` 的計算邏輯，回傳整理好的 rows。
    """
    pin_col = mdb.get_collection(pinuser_col)
    logs_col = mdb.get_collection(log_col)
    pur_col = mdb.get_collection(purchase_col)

    # 讀 PinUser 作為母集合（或 pins 指定清單）
    pin_docs = []
    if pins:
        pin_docs = list(pin_col.find({"pin_id": {"$in": pins}}, {"_id": 0}))
    else:
        pin_docs = list(pin_col.find({}, {"_id": 0}))

    # 初始化
    name: dict[int, str] = {}
    entries: dict[int, Any] = {}
    winnings: dict[int, Any] = {}
    pin_list: list[int] = []
    for doc in pin_docs:
        pid = doc.get("pin_id")
        if pid is None:
            continue
        try:
            pid_i = int(pid)
        except Exception:
            continue
        pin_list.append(pid_i)
        name[pid_i] = str(doc.get("first_name", "") or "")
        entries[pid_i] = doc.get("entries", 0)
        winnings[pid_i] = doc.get("winnings", 0)

    bet_total = {p: 0.0 for p in pin_list}
    jp_bet_total = {p: 0.0 for p in pin_list}
    win_total = {p: 0.0 for p in pin_list}
    jp_win = {p: 0.0 for p in pin_list}
    client_purchase = {p: 0.0 for p in pin_list}
    play_time = {p: 0 for p in pin_list}
    purchase = {p: 0.0 for p in pin_list}
    comps = {p: 0.0 for p in pin_list}
    purchase_out = {p: 0.0 for p in pin_list}

    # normalize range
    if start_dt_utc and end_dt_utc and end_dt_utc < start_dt_utc:
        start_dt_utc, end_dt_utc = end_dt_utc, start_dt_utc

    # 計算 logs + purchase
    log_tf: Optional[_TimeField] = None
    pur_tf: Optional[_TimeField] = None

    for pid in pin_list:
        # sum log data
        q_log: dict[str, Any] = {"PinID": pid}
        if start_dt_utc and end_dt_utc:
            if log_tf is None:
                log_tf = _detect_time_field(logs_col, {"PinID": pid})
            if log_tf is not None:
                q_log[log_tf.field] = {
                    "$gte": _to_range_value(start_dt_utc, log_tf.unit),
                    "$lte": _to_range_value(end_dt_utc, log_tf.unit),
                }

        for j in logs_col.find(q_log, {"_id": 0}):
            t = str(j.get("ThemeTitle", "") or "")
            if theme_title and (theme_title in t):
                if t in ("GoldenExpressGDBuyBonus_0", "DragonTreasure_1", "DragonTreasure_2", "DragonTreasure_4"):
                    bet_total[pid] += float(j.get("BuyBonusCost") or 0)
                else:
                    bet_total[pid] += float(j.get("GameBetCoin") or 0) + float(j.get("ExtraBet") or 0)
                jp_bet_total[pid] += float(j.get("JpBetCoin") or 0)
                win_total[pid] += float(j.get("WinAmount") or 0)
                play_time[pid] += 1
            elif t == "ClientPurchase":
                client_purchase[pid] -= float(j.get("WinAmount") or 0)
            elif t == "Jackpot":
                jp_win[pid] += float(j.get("WinAmount") or 0)

        # sum purchase data
        q_pur: dict[str, Any] = {"pin_id": pid}
        if start_dt_utc and end_dt_utc:
            if pur_tf is None:
                pur_tf = _detect_time_field(pur_col, {"pin_id": pid})
            if pur_tf is not None:
                q_pur[pur_tf.field] = {
                    "$gte": _to_range_value(start_dt_utc, pur_tf.unit),
                    "$lte": _to_range_value(end_dt_utc, pur_tf.unit),
                }

        for j in pur_col.find(q_pur, {"_id": 0}):
            tmp_purchase = float(j.get("purchase_amount") or 0) * 100.0
            add_entries = float(j.get("added_entries") or 0)
            decrease_winning = float(j.get("decrease_winnings") or 0)
            purchase_out[pid] += decrease_winning
            if decrease_winning <= 0:
                purchase[pid] += tmp_purchase
            if add_entries > 0:
                comps[pid] += add_entries - tmp_purchase

    rows: list[dict[str, Any]] = []
    for pid in pin_list:
        rows.append(
            {
                "lastname": name.get(pid, ""),
                "pin": pid,
                "betTotal": bet_total[pid],
                "jpBet": jp_bet_total[pid],
                "winTotal": win_total[pid],
                "jpWin": jp_win[pid],
                "purchase": purchase[pid],
                "playTime": play_time[pid],
                "clientPurchase": client_purchase[pid],
                "comps": comps[pid],
                "purchaseOut": purchase_out[pid],
                "entries": entries.get(pid, 0),
                "winnings": winnings.get(pid, 0),
                "net": (win_total[pid] + jp_win[pid]) - bet_total[pid],
            }
        )

    # sort: betTotal desc, then pin
    rows.sort(key=lambda r: (-float(r.get("betTotal") or 0), int(r.get("pin") or 0)))
    cols = [
        "lastname",
        "pin",
        "betTotal",
        "jpBet",
        "winTotal",
        "jpWin",
        "net",
        "purchase",
        "clientPurchase",
        "playTime",
        "comps",
        "purchaseOut",
        "entries",
        "winnings",
    ]
    return cols, rows


def _pick_numeric_columns(rows: list[dict[str, Any]], preferred: list[str]) -> list[_Option]:
    if not rows:
        return []
    cols = list(rows[0].keys())

    numeric = []
    for c in cols:
        ok = False
        for r in rows[:200]:
            x = _safe_float(r.get(c))
            if x is not None:
                ok = True
                break
        if ok:
            numeric.append(c)

    out: list[str] = []
    # preferred first
    for p in preferred:
        if p in numeric and p not in out:
            out.append(p)
    # then the rest
    for c in numeric:
        if c not in out:
            out.append(c)
    return [_Option(value=c, label=c) for c in out]


def _build_bar_figure(rows: list[dict[str, Any]], x_key: str, y_key: str, color_key: Optional[str]) -> "go.Figure":
    xs: list[str] = []
    ys: list[float] = []
    cs: list[str] = []

    for r in rows:
        x = r.get(x_key)
        if x is None:
            continue
        y = _safe_float(r.get(y_key))
        if y is None:
            continue
        xs.append(str(x))
        ys.append(float(y))
        cs.append(str(r.get(color_key) or "-") if color_key else "-")

    # group by color
    by_c: dict[str, dict[str, float]] = {}
    for x, y, c in zip(xs, ys, cs, strict=False):
        by_c.setdefault(c, {})
        by_c[c][x] = by_c[c].get(x, 0.0) + y

    x_order = sorted({*xs})
    fig = go.Figure()
    for c in sorted(by_c.keys()):
        fig.add_trace(go.Bar(name=c, x=x_order, y=[by_c[c].get(x, 0.0) for x in x_order]))

    fig.update_layout(
        title=f"{y_key}（依 {x_key}）",
        barmode="group",
        height=520,
        margin=dict(l=40, r=20, t=60, b=40),
    )
    fig.update_xaxes(tickangle=-35)
    return fig


def _handle_experience_meeting_analyze():
    db_env = (request.args.get("db_env", "macross-test") or "macross-test").strip()
    log_col = (request.args.get("log_col") or DEFAULT_LOG_COL).strip()
    theme_title = (request.args.get("game") or request.args.get("theme_title") or DEFAULT_THEME_TITLE).strip()
    pins_raw = (request.args.get("pins") or "").strip()
    start_dt_s = (request.args.get("start_dt") or "").strip()
    end_dt_s = (request.args.get("end_dt") or "").strip()
    metric = (request.args.get("metric") or "net").strip()

    warning = ""
    meta = ""
    table_rows: list[dict[str, Any]] = []
    table_cols: list[str] = []
    fig_html = ""
    fig_title = ""
    metric_options: list[_Option] = []
    export_url = ""

    try:
        db_env, preset, mdb = _get_mdb(db_env)
        pins = _parse_pins(pins_raw)
        start_dt_utc = _parse_dt_local(start_dt_s)
        end_dt_utc = _parse_dt_local(end_dt_s)
        cache_key = (
            db_env,
            log_col,
            theme_title,
            ",".join(map(str, pins)),
            start_dt_s,
            end_dt_s,
        )
        cached = _cache_get(cache_key)
        if cached:
            table_cols = cached.get("table_cols", [])
            table_rows = cached.get("table_rows", [])
            meta = (cached.get("meta") or "") + " | cache=HIT"
        else:
            table_cols, table_rows = _compute_experience_stats(
                mdb=mdb,
                pinuser_col=DEFAULT_PINUSER_COL,
                purchase_col=DEFAULT_PURCHASE_COL,
                log_col=log_col,
                theme_title=theme_title,
                pins=pins,
                start_dt_utc=start_dt_utc,
                end_dt_utc=end_dt_utc,
            )
            meta = (
                f"env={db_env} db={preset['mongo_db']} | log_col={log_col} | game={theme_title} | "
                f"pins={len(pins) if pins else 'ALL'} | range={start_dt_s or '-'}~{end_dt_s or '-'} | rows={len(table_rows)}"
            )
            _cache_set(cache_key, {"table_cols": table_cols, "table_rows": table_rows, "meta": meta})
            meta = meta + " | cache=MISS"

        x_key = "lastname" if table_rows and ("lastname" in table_rows[0]) else (table_cols[0] if table_cols else "lastname")
        color_key = None

        metric_options = _pick_numeric_columns(
            table_rows,
            preferred=["net", "winTotal", "betTotal", "jpWin", "purchase", "clientPurchase", "playTime", "comps", "entries", "winnings"],
        )
        if metric not in {o.value for o in metric_options}:
            metric = metric_options[0].value if metric_options else metric

        if table_rows and metric_options:
            fig = _build_bar_figure(table_rows, x_key=x_key, y_key=metric, color_key=color_key)
            fig_title = fig.layout.title.text or ""
            fig_html = fig.to_html(full_html=False, include_plotlyjs="cdn")

        # export link (same params)
        qs = request.args.to_dict(flat=True)
        qs["metric"] = metric
        # build a relative query string manually (avoid url_for external)
        from urllib.parse import urlencode

        export_url = "/ExperienceMeetingAnalyze/export.xlsx?" + urlencode(qs)
    except Exception as e:
        print("=== ExperienceMeetingAnalyze error ===")
        print(traceback.format_exc())
        warning = f"分析失敗：{type(e).__name__}: {e}"

    return render_template(
        TEMPLATE_NAME,
        brand_title="體驗會資料分析",
        brand_url="/ExperienceMeetingAnalyze",
        active_route="experience_meeting_analyze",
        show_env_select=True,
        db_env=db_env,
        db_presets=[e for e in list_envs("ExperienceMeetingAnalyzeWeb") if e != "default"],
        log_col=log_col,
        theme_title=theme_title,
        pins=pins_raw,
        start_dt=start_dt_s,
        end_dt=end_dt_s,
        metric=metric,
        warning=warning,
        meta=meta,
        table_cols=table_cols,
        table_rows=table_rows,
        metric_options=metric_options,
        fig_title=fig_title,
        fig_html=fig_html,
        export_url=export_url,
    )


def _handle_experience_meeting_export_xlsx():
    db_env = (request.args.get("db_env", "macross-test") or "macross-test").strip()
    log_col = (request.args.get("log_col") or DEFAULT_LOG_COL).strip()
    theme_title = (request.args.get("game") or request.args.get("theme_title") or DEFAULT_THEME_TITLE).strip()
    pins_raw = (request.args.get("pins") or "").strip()
    start_dt_s = (request.args.get("start_dt") or "").strip()
    end_dt_s = (request.args.get("end_dt") or "").strip()

    db_env, _, mdb = _get_mdb(db_env)
    pins = _parse_pins(pins_raw)
    cache_key = (
        db_env,
        log_col,
        theme_title,
        ",".join(map(str, pins)),
        start_dt_s,
        end_dt_s,
    )
    cached = _cache_get(cache_key)
    if cached:
        table_cols = cached.get("table_cols", [])
        table_rows = cached.get("table_rows", [])
    else:
        start_dt_utc = _parse_dt_local(start_dt_s)
        end_dt_utc = _parse_dt_local(end_dt_s)
        table_cols, table_rows = _compute_experience_stats(
            mdb=mdb,
            pinuser_col=DEFAULT_PINUSER_COL,
            purchase_col=DEFAULT_PURCHASE_COL,
            log_col=log_col,
            theme_title=theme_title,
            pins=pins,
            start_dt_utc=start_dt_utc,
            end_dt_utc=end_dt_utc,
        )
        _cache_set(cache_key, {"table_cols": table_cols, "table_rows": table_rows, "meta": ""})

    try:
        import pandas as pd  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"需要 pandas/openpyxl 才能匯出 xlsx：{e}")

    df = pd.DataFrame(table_rows)
    # keep column order
    if table_cols:
        cols2 = [c for c in table_cols if c in df.columns] + [c for c in df.columns if c not in table_cols]
        df = df[cols2]

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="ExperienceMeeting")
    buf.seek(0)

    safe_theme = "".join(ch for ch in (theme_title or "theme") if ch.isalnum() or ch in ("-", "_"))
    fname = f"experience_meeting_{safe_theme}_{log_col}.xlsx"
    return send_file(
        buf,
        as_attachment=True,
        download_name=fname,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def init_experience_meeting_analyze_routes(flask_app: Flask) -> None:
    """
    Register Experience Meeting analyze page routes onto an existing Flask app.

    - GET /ExperienceMeetingAnalyze
    - GET /ExperienceMeetingAnalyze/ (redirect-safe alias)
    """

    @flask_app.route("/ExperienceMeetingAnalyze", methods=["GET"], endpoint="experience_meeting_analyze")
    def experience_meeting_analyze():  # noqa: F811
        return _handle_experience_meeting_analyze()

    @flask_app.route("/ExperienceMeetingAnalyze/", methods=["GET"], endpoint="experience_meeting_analyze_slash")
    def experience_meeting_analyze_slash():  # noqa: F811
        return _handle_experience_meeting_analyze()

    @flask_app.route("/ExperienceMeetingAnalyze/export.xlsx", methods=["GET"], endpoint="experience_meeting_analyze_export_xlsx")
    def experience_meeting_analyze_export_xlsx():  # noqa: F811
        return _handle_experience_meeting_export_xlsx()

