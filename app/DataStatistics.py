# -*- coding: utf-8 -*-
from __future__ import annotations

__author__ = 'jackchen'

import pymongo
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from DBConnect import get_db_conn, get_mongo_client


@dataclass(frozen=True)
class ExperienceMeetingResult:
    """Parsed table from an Experience Meeting xlsx."""

    source_path: str
    rows: list[dict[str, Any]]
    columns: list[str]


class DataStatistic:
    def __init__(self):
        cfg = get_db_conn("DataStatistics", "default")
        mc = get_mongo_client(str(cfg.get("mongo_uri", "")).strip(), appname="my_web_etc_data_statistics")
        self.db = mc[str(cfg.get("mongo_db", "Data")).strip()]
        self.pinuserCol = self.db['PinUser']
        self.logCol = self.db['SessionGame_20250110']   # log 把 PinID 加入 index 可以加速搜尋時間
        self.purchaseHistory = self.db['PurchaseHistory']
        self.themeTitle = 'SkyKing'

    def startStatistic(self):
        pin, name, betTotal, jpBetTotal, winTotal, jpWin, clientPurchase, playTime = [], {}, {}, {}, {}, {}, {}, {}
        purchase, comps, purchaseOut, entries, winnings = {}, {}, {}, {}, {}

        pinCursor = self.pinuserCol.find()
        for i in pinCursor:
            tmpPin = i.get('pin_id')
            if tmpPin is not None:
                pin.append(tmpPin)
                name[tmpPin] = i.get('first_name', '')
                betTotal[tmpPin] = 0
                jpBetTotal[tmpPin] = 0
                winTotal[tmpPin] = 0
                jpWin[tmpPin] = 0
                clientPurchase[tmpPin] = 0
                playTime[tmpPin] = 0
                purchaseOut[tmpPin] = 0
                purchase[tmpPin] = 0
                comps[tmpPin] = 0
                entries[tmpPin] = i.get('entries', 0)
                winnings[tmpPin] = i.get('winnings', 0)

        print ('lastname\tpin\tbetTotal\tjpBet\twinTotal\tjpWin\tpurchase\tplayTime\tpurchase\tcomps\tpurchaseOut\tentries\twinnings')
        for i in pin:
            # sum log data
            logsCursor = self.logCol.find({'PinID': i})
            for j in logsCursor:
                themeTitle = str(j.get('ThemeTitle', ''))
                if self.themeTitle in themeTitle:
                    if themeTitle == "GoldenExpressGDBuyBonus_0":
                        betTotal[i] += j.get('BuyBonusCost')
                    elif themeTitle == "DragonTreasure_1":
                        betTotal[i] += j.get('BuyBonusCost')
                    elif themeTitle == "DragonTreasure_2":
                        betTotal[i] += j.get('BuyBonusCost')
                    elif themeTitle == "DragonTreasure_4":
                        betTotal[i] += j.get('BuyBonusCost')
                    else:
                        betTotal[i] += j.get('GameBetCoin', 0) + j.get('ExtraBet', 0)
                    jpBetTotal[i] += j.get('JpBetCoin', 0)
                    winTotal[i] += j.get('WinAmount', 0)
                    playTime[i] += 1
                elif themeTitle == 'ClientPurchase':
                    clientPurchase[i] -= j.get('WinAmount', 0)
                elif themeTitle == 'Jackpot':
                    jpWin[i] += j.get('WinAmount', 0)
                else:
                    print(j)
                    print('error!!!')

            # sum purchase data
            purchaseCursor = self.purchaseHistory.find({'pin_id': i})
            for j in purchaseCursor:
                tmpPurchase = j.get('purchase_amount', 0) * 100
                addEntries = j.get('added_entries', 0)
                decreaseWinning = j.get('decrease_winnings', 0)

                purchaseOut[i] += decreaseWinning
                if decreaseWinning <= 0:
                    purchase[i] += tmpPurchase
                if addEntries > 0:
                    comps[i] += addEntries - tmpPurchase

            print ('%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s' %
                   (name[i], i, betTotal[i], jpBetTotal[i], winTotal[i], jpWin[i], clientPurchase[i], playTime[i], purchase[i], comps[i], purchaseOut[i], entries[i], winnings[i]))


def parse_experience_meeting_xlsx(xlsx_path: str, sheet_name: str = "工作表1") -> ExperienceMeetingResult:
    """
    解析「體驗會」Excel（例如 `20260210_skyking_真錢測試.xlsx`）。

    `工作表1` 有兩層表頭：第 1 列是群組/大標題，第 2 列在部分欄位提供細項名稱（例如 BetTotal）。
    這裡會：
    - 用第 1 列作為 fallback 欄名
    - 若第 2 列在該欄有值且不是空/placeholder，就用第 2 列覆蓋欄名
    - 從第 3 列開始當資料（跳過兩層表頭）
    """
    try:
        import pandas as pd  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"需要 pandas/openpyxl 才能讀取 xlsx：{e}")

    p = Path(xlsx_path)
    if not p.exists():
        raise FileNotFoundError(str(p))

    raw = pd.read_excel(p, sheet_name=sheet_name, header=None)
    if raw.shape[0] < 3:
        return ExperienceMeetingResult(source_path=str(p), rows=[], columns=[])

    top = list(raw.iloc[0].tolist())
    sub = list(raw.iloc[1].tolist())

    def norm_name(v: Any) -> str:
        if v is None:
            return ""
        s = str(v).strip()
        return s

    cols: list[str] = []
    used: dict[str, int] = {}
    for i in range(len(top)):
        a = norm_name(sub[i]) if i < len(sub) else ""
        b = norm_name(top[i]) if i < len(top) else ""

        pick = a if a and not a.lower().startswith("unnamed") else b
        pick = pick or f"col_{i}"
        # de-dup
        n = used.get(pick, 0)
        used[pick] = n + 1
        cols.append(pick if n == 0 else f"{pick}.{n+1}")

    df = raw.iloc[2:].copy()
    df.columns = cols

    # drop fully-empty rows
    df = df.dropna(how="all")

    rows = df.to_dict(orient="records")
    return ExperienceMeetingResult(source_path=str(p), rows=rows, columns=cols)


if __name__ == '__main__':
    s = DataStatistic()
    s.startStatistic()
