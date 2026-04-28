from flask import Flask, render_template, request, redirect, url_for, jsonify
import webbrowser
from OpenWeb import getIpOption, getIpFromOption, getUrl
import json
import redis
from FishTeeYanAnalyzeWeb import init_play_fish_analyze_routes

app = Flask(__name__)

# Redis 連線設定
redis_client = redis.Redis(host='192.168.121.86', port=6379, db=0, decode_responses=True)

# Register Fish analyze web routes
init_play_fish_analyze_routes(app)


@app.route('/', methods=['GET'])
def index():
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
        # -------
        {"display": "雷霆戰機", "value": "SkyKing", "kind": "類魚"}, # 5901
        {"display": "萬聖派對(射繩)", "value": "HalloweenParty", "kind": "類魚"}, # 5902
    ]
    langs = ["en-us", "zh-cn"]
    kind_list = ["魚機", "聯名", "類魚"]
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
    return render_template('SifuLink.html', client_ips=client_ips, games=ordered_games, langs=langs, selected_client_ip=selected_client_ip, selected_server_ip=selected_server_ip)


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
        data = request.get_json()
        action = data.get('action')
        ark_id = data.get('arkId')
        
        # 驗證 Ark ID 格式
        if not ark_id or not ark_id.isdigit() or len(ark_id) != 8:
            return jsonify({
                'success': False,
                'message': 'Ark ID 必須是8位數字'
            }), 400
        
        redis_key = f"token_{ark_id}"
        
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
                'message': f'成功設定 {redis_key}',
                'key': redis_key,
                'token': ark_token
            })
        
        elif action == 'get':
            # 查詢 Token
            token = redis_client.get(redis_key)
            return jsonify({
                'success': True,
                'message': f'成功查詢 {redis_key}',
                'key': redis_key,
                'token': token
            })
        
        else:
            return jsonify({
                'success': False,
                'message': '不支援的操作'
            }), 400
            
    except redis.ConnectionError:
        return jsonify({
            'success': False,
            'message': 'Redis 連線失敗，請確認 Redis 服務是否啟動'
        }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'發生錯誤: {str(e)}'
        }), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5487, debug=True)
