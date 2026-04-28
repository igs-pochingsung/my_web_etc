# 魚機私服連結轉跳工具

一個專為魚機遊戲開發者設計的私服連結生成工具，支援多環境配置管理和參數模式儲存。

## 🎯 專案概述

這個工具主要用於生成魚機遊戲客戶端與伺服器之間的連接URL，支援多種魚機遊戲和不同的開發環境。開發者可以快速切換不同的配置，並儲存常用的參數組合。

## ✨ 主要功能

### 🔗 私服連結生成
- **多環境支援**: 支援多個客戶端IP和伺服器IP選擇
- **遊戲支援**: 支援16種魚機遊戲，包含中文名稱顯示
- **多語言**: 支援中文和英文介面
- **新分頁開啟**: 生成的連結會在新分頁中開啟

### 💾 參數記憶功能
- **自動保存**: 所有參數會自動保存到瀏覽器本地存儲
- **自動填入**: 頁面載入時自動填入上次使用的參數
- **跨會話保持**: 關閉瀏覽器後重新開啟仍會記住設定

### 🎛️ 參數模式管理
- **模式儲存**: 可儲存最多10組參數組合
- **快速切換**: 一鍵載入已儲存的參數模式
- **模式命名**: 為每個參數組合命名，方便識別
- **模式刪除**: 可刪除不需要的參數模式

### 🌐 多語言支援
- **中英文切換**: 支援中文和英文介面切換
- **語言記憶**: 語言選擇會自動保存
- **完整翻譯**: 所有介面元素都有對應的語言版本

### 🔑 Ark Token 管理
- **統一介面**: 查詢和設定共用同一個表單，操作更直覺
- **Token 查詢**: 根據 Ark ID 查詢對應的 Token，結果自動填入輸入框
- **Token 設定**: 設定 Ark ID 對應的 Token 到 Redis，設定前會顯示確認對話框
- **Token 修改**: 查詢後直接在輸入框修改即可更新
- **格式驗證**: 自動驗證 8 碼數字格式的 Ark ID
- **安全確認**: 設定前會彈出確認對話框，顯示完整設定內容
- **Redis 整合**: 直接操作 Redis DB 0
- **駭客風格**: 與 SifuLink 相同的 Matrix 雨滴動畫背景

## 🚀 快速開始

### 環境需求
- Python 3.9+
- Redis Server (用於 Ark Token 管理功能)
- Docker (可選)
- 現代瀏覽器 (支援localStorage)

### 使用Docker (推薦)

```bash
# 克隆專案
git clone <repository-url>
cd my_web_etc

# 啟動服務
docker compose up -d --build

# 確保服務重新build
docker compose build --no-cache 
docker compose up -d

# 關閉服務
docker compose down

# 開啟瀏覽器訪問
http://localhost:5487
```

### 直接運行Python

```bash
# 安裝依賴
pip install -r requirements.txt

# 啟動服務
cd app
python OpenWebFlaskApp.py

# 開啟瀏覽器訪問
http://localhost:5487
```

## 📖 使用說明

### 1. 基本連結生成

1. **選擇客戶端IP**: 從下拉選單選擇客戶端IP地址
2. **設定客戶端端口**: 預設為7456，可自定義修改
3. **選擇伺服器IP**: 從下拉選單選擇伺服器IP地址
4. **選擇遊戲**: 從遊戲列表中選擇要測試的魚機遊戲
5. **選擇語言**: 選擇遊戲語言 (en-us 或 zh-cn)
6. **輸入帳號**: 輸入測試帳號
7. **生成連結**: 點擊「請給我網址，為了魚機用途」按鈕

### 2. 參數模式管理

#### 儲存當前參數
1. 設定好所有參數後，在「輸入模式名稱」欄位輸入模式名稱
2. 點擊「💾 儲存當前參數」按鈕
3. 模式會保存到本地，最多可儲存10組

#### 載入已儲存的模式
1. 在已儲存模式列表中，點擊「🔄 載入」按鈕
2. 所有參數會自動填入對應欄位
3. 模式名稱會自動填入輸入框

#### 刪除模式
1. 在已儲存模式列表中，點擊「🗑️ 刪除」按鈕
2. 確認後模式會被刪除

### 3. 語言切換

- 點擊右上角的語言切換器
- 滑軌向左為中文，向右為英文
- 語言選擇會自動保存

### 4. Ark Token 管理

訪問 `http://localhost:5487/pocSIFU/setArk`

> 🎨 **介面風格**: 採用與 SifuLink 相同的駭客風格，黑底綠字配上 Matrix 雨滴動畫背景

#### 查詢 Token
1. 輸入 8 碼數字 Ark ID (例如: 10000001)
2. 點擊「查詢 Token」按鈕
3. 如果找到 Token，會**自動填入到 Ark Token 輸入框**
4. 如果沒有找到，會提示「尚未設定 Token」

#### 設定 Token
1. 輸入 8 碼數字 Ark ID
2. 在 Ark Token 輸入框輸入或修改 Token (hash 字串)
3. 點擊「設定 Token」按鈕
4. **確認對話框**會顯示設定內容：
   - Ark ID
   - Redis Key (token_arkId)
   - Token (長的話會截斷顯示前50字符)
5. 點擊「確定設定」完成設定，Token 會以 `token_arkId` 格式儲存到 Redis DB 0
6. 點擊「取消」可以放棄設定

#### 修改現有 Token
1. 先查詢現有的 Token（會自動填入輸入框）
2. 直接在輸入框中修改 Token 內容
3. 點擊「設定 Token」並確認
4. 完成更新

#### 技術細節
- **Redis 儲存格式**: `token_<8位ArkID>` → `<Token字串>`
- **資料庫**: Redis DB 0
- **主機**: localhost:6379
- **驗證**: 自動驗證 Ark ID 必須為 8 位數字
- **安全**: 設定前強制確認，避免誤操作

## 🎮 支援的遊戲

| 遊戲代碼 | 中文名稱 | 遊戲代碼 | 中文名稱 |
|---------|---------|---------|---------|
| ZombieAwaken | 惡靈覺醒 | AladdinAdventure | 阿拉丁冒險 |
| MrFortune | 至尊財神 | Circus | 馬戲團 |
| MoneyTree | 搖錢樹 | OceanParadise | 海洋開拓者 |
| LuckyBuddha | 彌勒佛 | SuperStar | 明星派對 |
| BlizzardDragon | 暴雪龍 | IceFire | 冰焰龍戰 |
| AgeOfFrost | 冰霜時代 | CatFish | 功夫喵 |
| JackpotFrenzy | 彩金狂飆 | RagingDragonDeluxe | 炙焰魔龍 |
| AladdinMirage | 阿拉丁2 | SkyKing | 雷霆戰機 |

## 🌍 支援的環境

### 客戶端IP
- Client - ICE (192.168.121.37)
- Client - JANE (192.168.121.192)
- Client - LEAVI (192.168.121.115)
- Client - DING (192.168.121.74)
- Client - C JER (192.168.121.150)
- Client - GOUWEI (192.168.123.121)
- Client - PAULO (192.168.121.108)

### 伺服器IP
- SERVER - POC (192.168.121.86)
- SERVER - KUO (192.168.121.139)
- PAN (192.168.121.45)
- DEV - 50 (192.168.121.50)
- DEV - 142 (192.168.121.142)
- DEV - Cross(macross-dev) (35.198.231.50)

## 🛠️ 技術架構

### 後端
- **框架**: Flask 1.1.4
- **語言**: Python 3.9
- **資料庫**: Redis (DB 0)
- **容器化**: Docker + Docker Compose

### 前端
- **HTML5**: 語義化標籤
- **CSS3**: 現代化樣式，支援響應式設計
- **JavaScript**: ES6+ 語法，模組化設計
- **動畫**: Canvas Matrix雨滴動畫效果

### 數據存儲
- **localStorage**: 瀏覽器本地存儲
- **參數記憶**: 自動保存用戶設定
- **模式管理**: 儲存參數組合

## 📁 專案結構

```
my_web_etc/
├── app/
│   ├── OpenWeb.py              # 核心邏輯模組
│   ├── OpenWebFlaskApp.py      # Flask應用程式
│   ├── FishTeeYanAnalyzeWeb.py # Fish 玩家遊玩狀況分析（掛載到 /PlayFishAnalyze）
│   ├── templates/
│   │   ├── SifuLink.html       # 主頁面模板
│   │   ├── FishEventSetting.html # 魚機活動設定工具
│   │   └── ArkTokenManager.html  # Ark Token管理工具
│   │   └── FishTeeYanAnalyze.html # Fish 玩家遊玩狀況分析頁模板
│   └── Tools/
│       └── ListChooser.py      # 輔助工具類
├── docker-compose.yml          # Docker編排配置
├── Dockerfile                  # Docker映像檔配置
├── requirements.txt            # Python依賴
└── README.md                   # 專案說明文件
```

## 📈 Fish 玩家遊玩狀況分析（Plotly + Mongo）

此分析頁已整合到主 Flask 服務中。

### 路由

- `GET /PlayFishAnalyze`
  - **遊玩時間查詢**：跨日查詢 `FishPlayerInOutTime_YYYYMMDD`（預設近 14 天，可用環境變數 `PLAYTIME_SEARCH_DAYS` 調整）
  - **詳細分析**：查詢 `SessionGame_YYYYMMDD` + `DetailBetWinFishRaw_YYYYMMDD` + `TransactionFishLog_YYYYMMDD` + `FishCollectDetailLog_YYYYMMDD`

### 啟動方式（直接跑 Python）

```bash
pip install -r requirements.txt
cd app
python OpenWebFlaskApp.py
```

然後開啟：

- 主頁：`http://localhost:5487/`
- 分析頁：`http://localhost:5487/PlayFishAnalyze`

### 需要的環境變數（可選）

- `MONGO_URI`: MongoDB 連線字串
- `MONGO_DB`: DB 名稱
- `INPUT_TZ_OFFSET_HOURS`: 時間輸入的時區偏移（預設 8）
- `PLAYTIME_SEARCH_DAYS`: 遊玩時間查詢往回天數（預設 14）

## 🔧 配置說明

### Docker配置
- **端口**: 5487 (可通過docker-compose.yml修改)
- **卷掛載**: 本地app目錄掛載到容器/code目錄
- **基礎映像**: python:3.9-alpine (輕量級)

### 環境變數
- 可通過修改docker-compose.yml添加環境變數
- 支援.env文件配置

## 🚨 注意事項

1. **瀏覽器相容性**: 需要支援localStorage的現代瀏覽器
2. **網路環境**: 確保客戶端和伺服器IP可以正常訪問
3. **參數限制**: 最多可儲存10組參數模式
4. **數據安全**: 所有數據只保存在用戶本地瀏覽器中
5. **Redis 連線**: Ark Token 管理功能需要 Redis 服務運行於 localhost:6379
6. **Ark ID 格式**: Ark ID 必須是 8 位數字 (例如: 10000001)
7. **設定確認**: 設定 Token 前會顯示確認對話框，請仔細檢查設定內容
8. **Token 覆蓋**: 設定新 Token 會覆蓋原有的 Token，請謹慎操作

## 🤝 貢獻指南

1. Fork 專案
2. 創建功能分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 開啟 Pull Request

## 📄 授權條款

本專案採用 MIT 授權條款 - 詳見 [LICENSE](LICENSE) 文件

## 📞 聯絡資訊

如有問題或建議，請透過以下方式聯絡：
- 開啟 Issue
- 發送 Pull Request
- 專案維護者聯絡方式

## 🔄 更新日誌

### v1.1.0
- 新增 Ark Token 管理工具
- 統一查詢和設定介面
- 查詢結果自動填入輸入框
- 設定前顯示確認對話框
- Matrix 雨滴動畫背景
- Redis DB 0 整合

### v1.0.0
- 初始版本發布
- 基本連結生成功能
- 參數記憶功能
- 參數模式管理
- 多語言支援
- Docker容器化部署

---

**享受使用魚機私服連結轉跳工具！** 🎮✨
