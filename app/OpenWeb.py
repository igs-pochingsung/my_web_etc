import webbrowser
from Tools.ListChooser import ListChooser

IP_MAP = {
    "Client - ICE": "192.168.121.37",
    "Client - JANE": "192.168.121.192",
    "Client - LEAVI": "192.168.121.115",
    "Client - DING": "192.168.121.74",
    "Client - C JER": "192.168.121.150",
    "Client - GOUWEI": "192.168.123.121",
    "Client - PAULO": "192.168.121.108",
    "Client - KIDD": "192.168.132.71",
    "Both - STEVELEE": "192.168.121.48",
    "SERVER - POC": "192.168.121.86",
    "SERVER - KUO": "192.168.121.139",
    "SERVER - SIMONWEI": "192.168.132.70",
    "PAN": "192.168.121.45",
    "DEV - 50": "192.168.121.50",
    "DEV - 142": "192.168.121.142",
    "DEV - Cross(macross-dev)": "35.198.231.50",
}
GAME_LIST = ["ZombieAwaken", "MrFortune", "MoneyTree", "LuckyBuddha", "BlizzardDragon", "AladdinAdventure", "Circus", "OceanParadise"]
URL = "http://{client_ip}:{client_port}/?devAccount={account}&currency=Coin&site=,http://{server_ip}:8080,,http://{server_ip}:18080,http://{server_ip}:9410&game={game}&InternalMode=true&lang={lang}"


def _local_mode():
    sorted_ip_key_list = sorted(IP_MAP.keys())
    sorted_ip_list = [IP_MAP[key] for key in sorted_ip_key_list]
    ipl = ListChooser(sorted_ip_list, sorted_ip_key_list, "Choose CLIENT IP")
    client_ip = ipl.choose()
    client_port = raw_input("Input client port[7457]: ")
    if not client_port:
        client_port = 7457
    ipl.pre_show = "Choose SERVER IP"
    server_ip = ipl.choose()
    gl = ListChooser(GAME_LIST, pre_show="Choose Game")
    game = gl.choose()
    account = raw_input("Input account[poc0001]: ")
    if not account:
        account = "poc0001"

    url = URL.format(client_ip=client_ip, client_port=client_port, server_ip=server_ip, account=account, game=game)
    print (
        "client_ip: {}, client_port: {}, server_ip: {}, game: {}, account: {}".format(client_ip, client_port, server_ip,
                                                                                      game, account))
    print ("url: {}".format(url))

    webbrowser.open(url)


def getIpOption():
    return sorted(IP_MAP.keys())


def getIpFromOption(option):
    return IP_MAP[option]


def getUrl(client_ip, client_port, server_ip, account, game, lang):
    return URL.format(client_ip=client_ip, client_port=client_port, server_ip=server_ip, account=account, game=game, lang=lang)


def _web_mode():
    pass


if __name__ == "__main__":
    if raw_input("Input W to run Web mode[default Local Mode]: ").upper() == "W":
        _web_mode()
    else:
        _local_mode()
