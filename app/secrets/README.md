# BigQuery 服務帳號金鑰

請將 GCP 下載的 JSON 金鑰放在此目錄，檔名須為：

```
macross-384902-bq-macross-report-custom-prod-df789cbd6aafa862a55b3b858d2480eb5e0e23df.json
```

完整路徑（本機）：

```
d:\_Docker\my_web_etc\app\secrets\macross-384902-bq-macross-report-custom-prod-df789cbd6aafa862a55b3b858d2480eb5e0e23df.json
```

Docker 容器內對應路徑：

```
/code/secrets/macross-384902-bq-macross-report-custom-prod-df789cbd6aafa862a55b3b858d2480eb5e0e23df.json
```

`DBConnect.py` 已設定 `bq_api_key` 指向上述路徑，放好檔案後重啟 web 服務即可。

**注意：** 此目錄下的 `*.json` 已加入 `.gitignore`，請勿將金鑰 commit 到 git。
