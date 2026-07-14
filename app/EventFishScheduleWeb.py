# -*- coding: utf-8 -*-
import os
import traceback
from datetime import datetime, timedelta, timezone

import pymongo
from flask import Flask, request, render_template

from DBConnect import get_db_conn, get_mongo_client, list_envs


TEMPLATE_NAME = "EventFishSchedule.html"
DISPLAY_TZ_OFFSET_HOURS = int(os.getenv("EVENT_FISH_SCHEDULE_TZ_OFFSET_HOURS", os.getenv("INPUT_TZ_OFFSET_HOURS", "8")))

_CLIENTS = {}


def _safe_str(v):
    return ("" if v is None else str(v)).strip()


def _parse_iso_date(s):
    """
    Accept:
    - YYYY-MM-DD
    - YYYY-MM-DDTHH:MM (local tz assumed)
    Return timezone-aware datetime (UTC).
    """
    s = _safe_str(s)
    if not s:
        return None
    try:
        if "T" in s:
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M")
        else:
            dt = datetime.strptime(s, "%Y-%m-%d")
        # interpret as local (UTC+offset), then convert to UTC
        local_tz = timezone(timedelta(hours=DISPLAY_TZ_OFFSET_HOURS))
        return dt.replace(tzinfo=local_tz).astimezone(timezone.utc)
    except Exception:
        return None


def _format_dt(dt):
    if not dt:
        return ""
    try:
        local_tz = timezone(timedelta(hours=DISPLAY_TZ_OFFSET_HOURS))
        return dt.astimezone(local_tz).strftime("%Y-%m-%d %H:%M") + f" (UTC{DISPLAY_TZ_OFFSET_HOURS:+d})"
    except Exception:
        return str(dt)


def _get_client(uri):
    if not uri:
        raise RuntimeError("Mongo URI 未設定。")
    cached = _CLIENTS.get(uri)
    if cached is not None:
        return cached
    client = get_mongo_client(uri, appname="my_web_etc_event_fish_schedule")
    client.admin.command("ping")
    _CLIENTS[uri] = client
    return client


def _base_fish(game_type):
    """
    從 InGameType 推估「魚種/遊戲大類」：
    - 去掉結尾 Happy / Regal
    - 其他字串保持
    """
    s = _safe_str(game_type)
    for suf in ("Happy", "Regal"):
        if s.endswith(suf) and len(s) > len(suf):
            return s[: -len(suf)]
    return s


def _handle_event_fish_schedule(
    db_env_override=None,
    show_env_select_override=None,
    brand_title_override=None,
    brand_url_override=None,
    active_route_override=None,
):
    db_env = (db_env_override or (request.args.get("db_env", "macross-test") or "macross-test")).strip()
    cfg = get_db_conn("EventFishScheduleWeb", db_env, default_env="macross-test")
    fishdb_uri = str(cfg.get("fishdb_mongo_uri", "")).strip()
    fishdb_stage_db = str(cfg.get("fishdb_stage_db_name", "Stage")).strip()
    fishdb_stage_script_db = str(cfg.get("fishdb_stage_script_db_name", "StageScript")).strip()

    warning = ""
    meta = ""

    # default time window: -7d to +60d (UTC)
    now_utc = datetime.now(timezone.utc)
    default_start = now_utc - timedelta(days=7)
    default_end = now_utc + timedelta(days=60)

    q_start = _parse_iso_date(request.args.get("start", "")) or default_start
    q_end = _parse_iso_date(request.args.get("end", "")) or default_end

    if q_end < q_start:
        q_start, q_end = q_end, q_start

    rows = []
    fish_groups = []  # [{fish, games:[{game, items:[] }]}]
    stage_game_names = []

    if not fishdb_uri:
        warning = "尚未設定 fishdb 連線（FISHDB_MONGO_URI）。"
        return render_template(
            TEMPLATE_NAME,
            brand_title=(brand_title_override or "EventFishSchedule"),
            brand_url=(brand_url_override or "/EventFishSchedule"),
            active_route=(active_route_override or "event_fish_schedule"),
            show_env_select=(True if show_env_select_override is None else bool(show_env_select_override)),
            db_env=db_env,
            db_presets=[e for e in list_envs("EventFishScheduleWeb") if e not in ("default", "macross-prod", "sssapi-prod")],
            warning=warning,
            meta="",
            q_start=_format_dt(q_start),
            q_end=_format_dt(q_end),
            start_val=q_start.astimezone(timezone(timedelta(hours=DISPLAY_TZ_OFFSET_HOURS))).strftime("%Y-%m-%d"),
            end_val=q_end.astimezone(timezone(timedelta(hours=DISPLAY_TZ_OFFSET_HOURS))).strftime("%Y-%m-%d"),
            fish_groups=[],
            stage_game_names=[],
        )

    try:
        cli = _get_client(fishdb_uri)

        # stage games list (for sorting reference)
        stage_db = cli[fishdb_stage_db]
        stage_col = stage_db.get_collection("Stage")
        stage_game_names = stage_col.distinct("Name", {}) or []
        stage_game_names = sorted([_safe_str(x) for x in stage_game_names if _safe_str(x)], key=str.lower)

        # schedules
        ss_db = cli[fishdb_stage_script_db]
        sch_col = ss_db.get_collection("ScriptSchedule")

        # overlap query
        q = {"StartTime": {"$lte": q_end}, "EndTime": {"$gte": q_start}}
        raw = list(
            sch_col.find(
                q,
                {
                    "_id": 0,
                    "Name": 1,
                    "Script": 1,
                    "StartTime": 1,
                    "EndTime": 1,
                    "InGameType": 1,
                    "ExcludeGameType": 1,
                },
            ).sort("StartTime", pymongo.ASCENDING)
        )

        # normalize & explode by InGameType (one row per game)
        for d in raw:
            st = d.get("StartTime")
            et = d.get("EndTime")
            ig = d.get("InGameType") if isinstance(d.get("InGameType"), list) else []
            ex = d.get("ExcludeGameType") if isinstance(d.get("ExcludeGameType"), list) else []
            if not isinstance(st, datetime) or not isinstance(et, datetime):
                continue
            for g in ig:
                gt = _safe_str(g)
                if not gt:
                    continue
                fish = _base_fish(gt)
                rows.append(
                    {
                        "fish": fish,
                        "game": gt,
                        "name": _safe_str(d.get("Name")),
                        "script": _safe_str(d.get("Script")),
                        "start": st,
                        "end": et,
                        "start_text": _format_dt(st),
                        "end_text": _format_dt(et),
                        "exclude": [str(x) for x in ex if _safe_str(x)],
                    }
                )

        # sorting: fish -> game -> start
        stage_rank = {n: i for i, n in enumerate(stage_game_names)}

        def game_rank(game):
            # if Stage.Stage has exact game name: use it; else rank by base fish name
            if game in stage_rank:
                return stage_rank[game]
            return 10**9

        rows.sort(key=lambda r: (r.get("fish") or "", game_rank(r.get("game") or ""), r.get("game") or "", r.get("start") or now_utc))

        # compute timeline positions (0..100) within window
        span_sec = max(1.0, (q_end - q_start).total_seconds())
        for r in rows:
            left = max(0.0, (r["start"] - q_start).total_seconds()) / span_sec * 100.0
            right = max(0.0, (r["end"] - q_start).total_seconds()) / span_sec * 100.0
            width = max(0.5, min(100.0, right) - min(100.0, left))
            r["left_pct"] = max(0.0, min(100.0, left))
            r["width_pct"] = max(0.5, min(100.0, width))

        # group
        fish_map = {}
        for r in rows:
            fish = r["fish"]
            game = r["game"]
            fish_node = fish_map.setdefault(fish, {})
            fish_node.setdefault(game, []).append(r)

        fish_groups = []
        for fish in sorted(fish_map.keys(), key=str.lower):
            games = []
            for game in sorted(fish_map[fish].keys(), key=lambda g: (game_rank(g), g.lower())):
                games.append({"game": game, "items": fish_map[fish][game]})
            fish_groups.append({"fish": fish, "games": games})

        meta = f"window={_format_dt(q_start)} ~ {_format_dt(q_end)} | schedules={len(raw)} | rows={len(rows)}"
    except Exception as e:
        print("=== EventFishSchedule query error ===")
        print(traceback.format_exc())
        warning = f"查詢失敗：{type(e).__name__}: {e}"

    return render_template(
        TEMPLATE_NAME,
        brand_title=(brand_title_override or "EventFishSchedule"),
        brand_url=(brand_url_override or "/EventFishSchedule"),
        active_route=(active_route_override or "event_fish_schedule"),
        show_env_select=(True if show_env_select_override is None else bool(show_env_select_override)),
        db_env=db_env,
        db_presets=[e for e in list_envs("EventFishScheduleWeb") if e not in ("default", "macross-prod", "sssapi-prod")],
        warning=warning,
        meta=meta,
        q_start=_format_dt(q_start),
        q_end=_format_dt(q_end),
        start_val=q_start.astimezone(timezone(timedelta(hours=DISPLAY_TZ_OFFSET_HOURS))).strftime("%Y-%m-%d"),
        end_val=q_end.astimezone(timezone(timedelta(hours=DISPLAY_TZ_OFFSET_HOURS))).strftime("%Y-%m-%d"),
        fish_groups=fish_groups,
        stage_game_names=stage_game_names,
    )


def init_event_fish_schedule_routes(flask_app):
    @flask_app.route("/EventFishSchedule", methods=["GET"], endpoint="event_fish_schedule")
    def event_fish_schedule():  # noqa: F811
        return _handle_event_fish_schedule()

    @flask_app.route("/EventFishSchedule/", methods=["GET"], endpoint="event_fish_schedule_slash")
    def event_fish_schedule_slash():  # noqa: F811
        return _handle_event_fish_schedule()

    @flask_app.route("/EventFishScheduleMacrossProd", methods=["GET"], endpoint="event_fish_schedule_macross_prod")
    def event_fish_schedule_macross_prod():  # noqa: F811
        return _handle_event_fish_schedule(
            db_env_override="macross-prod",
            show_env_select_override=False,
            brand_title_override="[Macross正式環境]EventFishSchedule",
            brand_url_override="/EventFishScheduleMacrossProd",
            active_route_override="event_fish_schedule_macross_prod",
        )

    @flask_app.route("/EventFishScheduleSssapiProd", methods=["GET"], endpoint="event_fish_schedule_sssapi_prod")
    def event_fish_schedule_sssapi_prod():  # noqa: F811
        return _handle_event_fish_schedule(
            db_env_override="sssapi-prod",
            show_env_select_override=False,
            brand_title_override="[Sssapi正式環境]EventFishSchedule",
            brand_url_override="/EventFishScheduleSssapiProd",
            active_route_override="event_fish_schedule_sssapi_prod",
        )

