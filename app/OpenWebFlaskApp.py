# -*- coding: utf-8 -*-
from flask import Flask, render_template, request, redirect, url_for, jsonify, make_response
import webbrowser
from OpenWeb import getIpOption, getIpFromOption, getUrl
import json
from datetime import datetime
import traceback
import time
import uuid

try:
    import pymongo  # type: ignore
except Exception:  # pragma: no cover
    pymongo = None  # type: ignore
from FishTeeYanAnalyzeWeb import init_play_fish_analyze_routes
from ExperienceMeetingAnalyzeWeb import init_experience_meeting_analyze_routes
from BufferKioskInfoWeb import init_buffer_kiosk_info_routes
from EventFishScheduleWeb import init_event_fish_schedule_routes

app = Flask(__name__)

# --- connect_prod warning gate / local logging ---
PROD_ACK_COOKIE = "my_web_etc_prod_ack"
PROD_ACK_MAX_AGE_SEC = 24 * 60 * 60
_LOCAL_MONGO_CLIENT = None
_PROD_ACK_TOKENS = {}
# token 有效期間內可重複使用（支援同頁面內 API 分頁載入）
PROD_ACK_TOKEN_TTL_SEC = 10 * 60


def _no_cache(resp):
    """
    避免警告頁被瀏覽器/代理快取而造成「看似可跳過」的情況。
    """
    try:
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    except Exception:
        pass
    return resp


def _is_truthy(v):
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def _get_connect_prod():
    # 不依賴 connect_prod 參數（避免被繞過）
    # 規則：
    # - 若 db_env=macross-prod => 視為 connect_prod
    # - 若 endpoint 屬於明確的 prod page => 視為 connect_prod
    # - 若 path 屬於明確的 prod page（保險） => 視為 connect_prod
    try:
        if str(request.args.get("db_env") or "").strip().lower() in ("macross-prod", "sssapi-prod"):
            return True
    except Exception:
        pass
    try:
        if str(request.path or "").startswith("/KioskBufferInfoMacrossProd"):
            return True
    except Exception:
        pass
    try:
        if str(request.path or "").startswith("/KioskBufferInfoSssapiProd"):
            return True
    except Exception:
        pass
    ep = getattr(request, "endpoint", None)
    if ep in ("kiosk_buffer_info_macross_prod", "kiosk_buffer_info_sssapi_prod"):
        return True
    return False


def _get_local_mongo():
    global _LOCAL_MONGO_CLIENT
    if pymongo is None:
        return None
    if _LOCAL_MONGO_CLIENT is None:
        _LOCAL_MONGO_CLIENT = pymongo.MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=800)
    return _LOCAL_MONGO_CLIENT


def _log_connect(event, extra=None):
    """
    記錄連線資訊到 localdb:
      localhost:27017 / my_web_etc.ConnectLog
    """
    try:
        cli = _get_local_mongo()
        if cli is None:
            return
        db = cli["my_web_etc"]
        col = db.get_collection("ConnectLog")
        doc = {
            "ts_utc": datetime.utcnow(),
            "event": event,
            "remote_addr": request.headers.get("X-Forwarded-For", request.remote_addr),
            "method": request.method,
            "path": request.path,
            "full_path": request.full_path,
            "query": dict(request.args),
            "user_agent": request.headers.get("User-Agent", ""),
            "referer": request.headers.get("Referer", ""),
            "connect_prod": True,
        }
        if extra and isinstance(extra, dict):
            doc.update(extra)
        col.insert_one(doc)
    except Exception:
        # logging should never break UX
        print("=== ConnectLog insert failed ===")
        print(traceback.format_exc())


@app.context_processor
def inject_connect_prod():
    return {"connect_prod": _get_connect_prod()}


@app.before_request
def prod_warning_gate():
    if not _get_connect_prod():
        return None

    # allow warning endpoints
    if request.path.startswith("/ProdWarning"):
        return None

    # one-time bypass token (short TTL), so we can "always show warning" without relying on cookies
    token = request.args.get("__prod_ack") or ""
    if token:
        now = time.time()
        # purge expired
        expired = [k for k, exp in _PROD_ACK_TOKENS.items() if exp <= now]
        for k in expired:
            _PROD_ACK_TOKENS.pop(k, None)
        exp = _PROD_ACK_TOKENS.get(token)
        if exp and exp > now:
            # token 在 TTL 內可重複使用（不消耗）
            _log_connect("page_view", {"ack_token": "ok"})
            return None

    # keep original url (no-cache redirect)
    next_url = request.full_path
    if next_url.endswith("?"):
        next_url = next_url[:-1]
    return _no_cache(redirect(url_for("prod_warning", next=next_url)))

    # unreachable


@app.route("/ProdWarning", methods=["GET"], endpoint="prod_warning")
def prod_warning():
    nxt = request.args.get("next") or "/"
    cancel = "/"
    try:
        cancel = request.headers.get("Referer") or "/"
    except Exception:
        cancel = "/"
    resp = make_response(
        render_template(
        "ProdWarning.html",
        # topbar context
        brand_title="正式機風險提示",
        brand_url=url_for("index"),
        active_route="prod_warning",
        show_env_select=False,
        db_env=(request.args.get("db_env", "macross-test") or "macross-test").strip(),
        db_presets=["macross-test", "sssapi-test"],
        next_url=nxt,
        cancel_url=cancel,
        )
    )
    return _no_cache(resp)


@app.route("/ProdWarning/accept", methods=["POST"], endpoint="prod_warning_accept")
def prod_warning_accept():
    agree = _is_truthy(request.form.get("agree"))
    nxt = request.form.get("next") or "/"
    if not agree:
        return _no_cache(redirect(url_for("prod_warning", next=nxt)))

    _log_connect("ack", {"next": nxt})
    # create short-lived one-time token
    token = uuid.uuid4().hex
    _PROD_ACK_TOKENS[token] = time.time() + PROD_ACK_TOKEN_TTL_SEC

    # append token to next url
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

        sp = urlsplit(nxt)
        q = dict(parse_qsl(sp.query, keep_blank_values=True))
        q["__prod_ack"] = token
        nxt2 = urlunsplit((sp.scheme, sp.netloc, sp.path, urlencode(q, doseq=True), sp.fragment))
    except Exception:
        sep = "&" if ("?" in nxt) else "?"
        nxt2 = nxt + sep + "__prod_ack=" + token

    resp = make_response(redirect(nxt2))
    # do not set long-lived cookie (always show warning)
    try:
        resp.delete_cookie(PROD_ACK_COOKIE)
    except Exception:
        pass
    return _no_cache(resp)

# Redis 連線設定
try:
    import redis  # type: ignore
except Exception:  # pragma: no cover
    redis = None  # type: ignore
    redis_client = None
else:
    try:
        from DBConnect import get_redis_client
        redis_client = get_redis_client()
    except Exception:  # pragma: no cover
        redis_client = None

# Register Fish analyze web routes
init_play_fish_analyze_routes(app)
# Register Experience Meeting analyze routes
init_experience_meeting_analyze_routes(app)
# Register Kiosk Buffer info route (not in topbar list)
init_buffer_kiosk_info_routes(app)
# Register EventFishSchedule routes
init_event_fish_schedule_routes(app)


@app.route('/api/ip_list', methods=['GET'])
def api_ip_list():
    """回傳可用的 IP 選項清單，供 Agent Skill 動態查詢"""
    return jsonify({
        'ip_options': getIpOption(),
        'games': [
            {"code": "ZombieAwaken",           "name": "惡靈覺醒",         "type": "魚機"},
            {"code": "LuckyBuddha",            "name": "彌勒佛",           "type": "魚機"},
            {"code": "MrFortune",              "name": "至尊財神",         "type": "魚機"},
            {"code": "MoneyTree",              "name": "搖錢樹",           "type": "魚機"},
            {"code": "IceFire",                "name": "冰焰龍戰",         "type": "魚機"},
            {"code": "AladdinAdventure",       "name": "阿拉丁冒險",       "type": "魚機"},
            {"code": "Circus",                 "name": "馬戲團",           "type": "魚機"},
            {"code": "SuperStar",              "name": "明星派對",         "type": "魚機"},
            {"code": "OceanParadise",          "name": "海洋開拓者",       "type": "魚機"},
            {"code": "AgeOfFrost",             "name": "泰坦冰紀元",       "type": "魚機"},
            {"code": "CatFish",                "name": "功夫喵",           "type": "魚機"},
            {"code": "JackpotFrenzy",          "name": "彩金狂飆",         "type": "魚機"},
            {"code": "RagingDragonDeluxe",     "name": "炙焰魔龍",         "type": "魚機"},
            {"code": "AladdinMirage",          "name": "阿拉丁2",          "type": "魚機"},
            {"code": "GoldenBuffalo",          "name": "野牛",             "type": "魚機"},
            {"code": "TycoonLegends",          "name": "富豪傳說",         "type": "魚機"},
            {"code": "PiggyFortunes",          "name": "三隻小豬",         "type": "魚機"},
            {"code": "ChampionShot",           "name": "冠軍射門",         "type": "魚機"},
            {"code": "Mummy",                  "name": "木乃伊",           "type": "魚機"},
            {"code": "SkyKing",                "name": "雷霆戰機",         "type": "類魚"},
            {"code": "HalloweenParty",         "name": "萬聖派對",         "type": "類魚"},
            {"code": "GhostHunters",           "name": "魔鬼剋星",         "type": "類魚"},
            {"code": "SuperStarHighRoller",    "name": "明星派對高廳館",   "type": "高分"},
            {"code": "CatFishHighRoller",      "name": "功夫喵高廳館",     "type": "高分"},
            {"code": "AladdinMirageHighRoller","name": "阿拉丁2高廳館",    "type": "高分"},
            {"code": "TycoonLegendsHighRoller","name": "富豪傳說高廳館",   "type": "高分"},
            {"code": "SkyKingHighRoller",      "name": "雷霆戰機高廳館",   "type": "類魚"},
        ],
        'langs': ['en-us', 'zh-cn'],
    })


@app.route('/', methods=['GET'])
def index():
    db_env = (request.args.get("db_env", "macross-test") or "macross-test").strip()
    client_ips = getIpOption()
    selected_client_ip = "Client - ICE"
    selected_server_ip = "DEV - Cross(macross-dev)"
    games = [
        # 5701
        {"display": "惡靈覺醒", "value": "ZombieAwaken"}, # 5702
        {"display": "彌勒佛", "value": "LuckyBuddha"}, # 5703
        # 5704
        {"display": "至尊財神", "value": "MrFortune"}, # 5705
        {"display": "搖錢樹", "value": "MoneyTree"}, # 5706
        {"display": "冰焰龍戰", "value": "IceFire"}, # 5707
        {"display": "阿拉丁冒險", "value": "AladdinAdventure"}, # 5708
        {"display": "馬戲團", "value": "Circus"}, # 5709
        {"display": "明星派對", "value": "SuperStar"}, # 5710
        {"display": "海洋開拓者", "value": "OceanParadise"}, # 5711
        {"display": "泰坦冰紀元", "value": "AgeOfFrost"}, # 5712
        {"display": "功夫喵", "value": "CatFish"}, # 5713
        {"display": "彩金狂飆", "value": "JackpotFrenzy"}, # 5714
        {"display": "炙焰魔龍", "value": "RagingDragonDeluxe"}, # 5715
        {"display": "阿拉丁2", "value": "AladdinMirage"}, # 5716
        {"display": "野牛", "value": "GoldenBuffalo"}, # 5717
        {"display": "富豪傳說", "value": "TycoonLegends"}, # 5718
        {"display": "三隻小豬", "value": "PiggyFortunes"}, # 5719
        {"display": "至尊財神", "value": "MrFortuneTree"}, # 5720
        {"display": "冠軍射門", "value": "ChampionShot"}, # 5721
        {"display": "木乃伊", "value": "Mummy"}, # 5722
        {"display": "冠軍射門 X JK8", "value": "ChampionShotJK8", "kind": "聯名"}, # 5724
        {"display": "冠軍射門 X SyarikatMR", "value": "ChampionShotSyarikatMR", "kind": "聯名"}, # 5725
        {"display": "冠軍射門 X SyarikatCUCI", "value": "ChampionShotSyarikatCUCI", "kind": "聯名"}, # 5726
        {"display": "彩金狂飆 X JK8", "value": "JackpotFrenzyJK8", "kind": "聯名"}, # 5727
        {"display": "明星派對高廳館", "value": "SuperStarHighRoller", "kind": "高分"}, # 5728
        {"display": "功夫喵高廳館", "value": "CatFishHighRoller", "kind": "高分"}, # 5729
        {"display": "阿拉丁2高廳館", "value": "AladdinMirageHighRoller", "kind": "高分"}, # 5730
        {"display": "富豪傳說高廳館", "value": "TycoonLegendsHighRoller", "kind": "高分"}, # 5731
        {"display": "三隻小豬高廳館", "value": "PiggyFortunesHighRoller", "kind": "高分"}, # 5732
        {"display": "冠軍射門 X 100VIP", "value": "ChampionShot100VIP", "kind": "聯名"}, # 5733
        {"display": "冠軍射門高廳館", "value": "ChampionShotHighRoller", "kind": "高分"}, # 5734
        # -------
        {"display": "雷霆戰機", "value": "SkyKing", "kind": "類魚"}, # 5901
        {"display": "萬聖派對(射繩)", "value": "HalloweenParty", "kind": "類魚"}, # 5902
        {"display": "雷霆戰機高廳館", "value": "SkyKingHighRoller", "kind": "類魚"}, # 5903
        {"display": "魔鬼剋星", "value": "GhostHunters", "kind": "類魚"}, # 5904
        {"display": "魔鬼剋星高廳館", "value": "GhostHuntersHighRoller", "kind": "類魚"}, # 5905

    ]
    langs = ["en-us", "zh-cn"]
    kind_list = ["魚機", "聯名", "高分", "類魚"]
    tmp = {k: [] for k in kind_list}
    for g in games:
        k = g.pop("kind","魚機")
        if k not in kind_list:
            continue
        tmp[k].append(g)
    ordered_games = []
    for k in kind_list:
        ordered_games.append({"display": k, "value": "- - - - - -"})
        ordered_games.extend(sorted(tmp.get(k,[]), key=lambda x: x['value']))
    ordered_games = [{"display": "{} - {}".format(game['value'], game['display']), "value": game['value']} for game in ordered_games]
    return render_template(
        'SifuLink.html',
        client_ips=client_ips,
        games=ordered_games,
        langs=langs,
        selected_client_ip=selected_client_ip,
        selected_server_ip=selected_server_ip,
        # topbar context
        brand_title="魚機私服連結轉跳",
        brand_url=url_for("index"),
        active_route="index",
        show_env_select=False,
        db_env=db_env,
        db_presets=["macross-test", "sssapi-test"],
    )


@app.route('/generate_url', methods=['POST'])
def generate_url():
    client_ip = getIpFromOption(request.form['client_ip'])
    client_port = request.form.get('client_port', 7456)  # Default port changed to 7456
    server_ip = getIpFromOption(request.form['server_ip'])
    account = request.form['account']
    if not account:  # Check if account is empty
        return "Account cannot be empty", 400  # Return an error message
    game = request.form['game']
    lang = request.form['lang']
    url = getUrl(client_ip, client_port, server_ip, account, game, lang)
    print(url)
    return redirect(url)


@app.route('/GetFishSetting', methods=['POST'])
def get_fish_setting():
    data = request.get_json()
    user_id = data.get('UserId')

    # Mock response data
    response_data = {
        "Currency": "THB",
        "Multiple": 0.01,
        "FuncSwitch": [1, 1, 1],
        "BetData": {
            "happy": ["1", "5", "10", "20", "50"],
            "regal": ["50", "100", "150", "200"]
        },
        "AssetLimit": {
            "happy": None,
            "regal": None
        },
        "DefaultBetIndex": {
            "happy": 0,
            "regal": 0
        },
        "Ratio": 10,
        "RTP": 0.98
    }

    return json.dumps(response_data)


@app.route('/EventSettingTool/Fish', methods=['GET'])
def fish_event_setting_tool():
    return render_template("FishEventSetting.html")


@app.route('/api/open_link', methods=['GET'])
def api_open_link():
    """
    Agent-friendly endpoint：帶參數呼叫即可在本機開啟私服連結。
    Query params:
      client  - Client IP option name (e.g. "Client - ICE")
      server  - Server IP option name (e.g. "DEV - Cross(macross-dev)")
      account - player account
      game    - game code (e.g. "ZombieAwaken")
      lang    - "en-us" or "zh-cn" (default: "en-us")
      port    - client port (default: 7456)
    """
    try:
        client_opt = request.args.get('client', 'Client - ICE')
        server_opt = request.args.get('server', 'DEV - Cross(macross-dev)')
        account    = request.args.get('account', '')
        game       = request.args.get('game', '')
        lang       = request.args.get('lang', 'en-us')
        port       = request.args.get('port', 7456)

        if not account:
            return jsonify({'success': False, 'message': 'account 不能為空'}), 400
        if not game:
            return jsonify({'success': False, 'message': 'game 不能為空'}), 400

        client_ip = getIpFromOption(client_opt)
        server_ip = getIpFromOption(server_opt)
        url = getUrl(client_ip, port, server_ip, account, game, lang)
        webbrowser.open(url)
        return jsonify({'success': True, 'url': url})
    except KeyError as e:
        return jsonify({'success': False, 'message': '找不到 IP 選項: {}'.format(str(e))}), 400
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/download/skill', methods=['GET'])
def download_skill():
    """動態組裝 Agent Skill zip 下載（每次請求從 SKILL.md 重新打包，永遠是最新版）"""
    import os, io, zipfile
    from flask import Response
    skill_md = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'skills', 'sifulink', 'SKILL.md')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.write(skill_md, 'sifulink/SKILL.md')
    buf.seek(0)
    return Response(
        buf.read(),
        mimetype='application/zip',
        headers={
            'Content-Disposition': 'attachment; filename="sifulink.zip"',
            'Cache-Control': 'no-store, no-cache, must-revalidate',
            'Pragma': 'no-cache',
        }
    )


@app.route('/FishBetWinSimulator', methods=['GET'])
def fish_bet_win_simulator():
    return render_template(
        "FishBetWinSimulator.html",
        # topbar context
        brand_title="魚機注單模擬器",
        brand_url=url_for("index"),
        active_route="fish_bet_win_simulator",
        show_env_select=False,
        db_env=(request.args.get("db_env", "macross-test") or "macross-test").strip(),
        db_presets=["macross-test", "sssapi-test"],
    )


@app.route('/pocSIFU/setArk', methods=['GET', 'POST'])
def set_ark():
    """
    Ark Token 管理路由
    GET: 顯示管理頁面
    POST: 設定或查詢 Token
    """
    if request.method == 'GET':
        return render_template("ArkTokenManager.html")
    
    # POST 請求處理
    try:
        if redis_client is None:
            return jsonify({
                'success': False,
                'message': 'Redis 套件/連線未設定（請先安裝 redis 套件並確認 Redis 服務）。'
            }), 500

        data = request.get_json()
        action = data.get('action')
        ark_id = data.get('arkId')
        
        # 驗證 Ark ID 格式
        if not ark_id or not ark_id.isdigit() or len(ark_id) != 8:
            return jsonify({
                'success': False,
                'message': 'Ark ID 必須是8位數字'
            }), 400
        
        redis_key = "token_{}".format(ark_id)
        
        if action == 'set':
            # 設定 Token
            ark_token = data.get('arkToken')
            if not ark_token:
                return jsonify({
                    'success': False,
                    'message': 'Ark Token 不能為空'
                }), 400
            
            redis_client.set(redis_key, ark_token)
            return jsonify({
                'success': True,
                'message': '成功設定 {}'.format(redis_key),
                'key': redis_key,
                'token': ark_token
            })
        
        elif action == 'get':
            # 查詢 Token
            token = redis_client.get(redis_key)
            return jsonify({
                'success': True,
                'message': '成功查詢 {}'.format(redis_key),
                'key': redis_key,
                'token': token
            })
        
        else:
            return jsonify({
                'success': False,
                'message': '不支援的操作'
            }), 400
            
    except Exception as e:
        # 如果有 redis module，優先辨識 ConnectionError
        if redis is not None and isinstance(e, getattr(redis, "ConnectionError", Exception)):
            return jsonify({
                'success': False,
                'message': 'Redis 連線失敗，請確認 Redis 服務是否啟動'
            }), 500
        return jsonify({
            'success': False,
            'message': '發生錯誤: {}'.format(str(e))
        }), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5487, debug=True)
