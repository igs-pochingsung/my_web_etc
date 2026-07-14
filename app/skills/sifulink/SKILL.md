---
name: sifulink
description: >
  Use this skill when the user wants to open a fish game private server URL,
  connect to a test/dev environment, or generate a game launch link.
  Triggers on phrases like "幫我開"、"開私服"、"開連結"、"連接遊戲"、"開測試連結".
version: "1.2"
author: IGS
---

# sifulink — 魚機私服連結轉跳

## Base URL

```
http://192.168.121.86:5487
```

## Workflow

### Step 1 — Always fetch options first (never hardcode)

Before doing anything else, call:

```
GET http://192.168.121.86:5487/api/ip_list
```

This returns the live list of IPs, games, and languages:

```json
{
  "ip_options": ["Both - STEVELEE", "Client - DING", "Client - ICE", ...],
  "games": [
    {"code": "AladdinMirage", "name": "阿拉丁2", "type": "魚機"},
    {"code": "SkyKing",       "name": "雷霆戰機", "type": "類魚"},
    ...
  ],
  "langs": ["en-us", "zh-cn"]
}
```

Use ONLY values from this response. Do not guess or use values not present in the response.

### Step 2 — Resolve user input against fetched data

Map what the user said to values returned by the API:

- **Client / Server**: match the user's mention (e.g. "ICE", "KIDD") against `ip_options` using substring or fuzzy match
- **Game**: match the user's mention (Chinese name or English code) against `games[].name` or `games[].code`; use the matched `games[].code` when calling the API
- **Account**: must be provided by the user — never assume or default
- **Language**: default `en-us` unless user specifies; must be one of the fetched `langs`
- **Port**: default `7456` unless user specifies

If a match is ambiguous or not found, present the relevant options to the user and ask them to choose.

### Step 3 — Open the link

```
GET http://192.168.121.86:5487/api/open_link?client={client}&server={server}&account={account}&game={game}&lang={lang}&port={port}
```

- `client` and `server` are the exact strings from `ip_options` (URL-encoded)
- `game` is the exact `code` from the fetched `games` list (URL-encoded)

On success the service opens the browser and returns:
```json
{ "success": true, "url": "http://..." }
```

Report the URL back to the user.

## Example conversations

**User:** 幫我開 ICE 的阿拉丁2，帳號 poc0001
1. `GET /api/ip_list` → get live `ip_options` and `games`
2. Find ip_option containing "ICE" → e.g. `"Client - ICE"`
3. Find game where name matches "阿拉丁2" → use its `code`
4. `GET /api/open_link?client=Client+-+ICE&server=...&account=poc0001&game={code}&lang=en-us`

**User:** 開 KIDD 的雷霆戰機
1. `GET /api/ip_list`
2. Match "KIDD" in ip_options, match "雷霆戰機" in games
3. Ask: "請問帳號是？"
4. After user replies → call `/api/open_link`

**User:** 我想連私服
1. `GET /api/ip_list`
2. Ask: "請問要連哪個 Client？哪個 Server？開哪款遊戲？帳號是？"
3. Show options from the fetched list for user to choose
4. After user replies → call `/api/open_link`
