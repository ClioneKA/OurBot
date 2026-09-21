# OurBot

OurBot 是以 `discord.py` 開發的 Discord 機器人，主要包含安安的 AI 對話、個人記憶、圖片／語音回覆，以及「安安大冒險」RPG。RPG 已涵蓋角色養成、自動討伐、手動團戰、生活技能、酒館、遠征、繪境迷宮、魔女試煉、魔女安息與煉金人偶。玩家也可以直接提及或回覆安安，詢問「鐵核重鎚哪裡拿？」、「浮光珍珠怎麼取得？」等道具來源及其他玩法規則；安安會從目前的玩家規則中找出相關內容回答。

## 快速開始

需求：Python 3.11 以上、Discord Bot Token；使用 AI 或語音時另需對應的 OpenAI、MiniMax 與 FFmpeg 設定。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe .\ourbot.py
```

接著依部署環境填寫 `.env`，並在 `config/settings.toml` 調整機器人行為。密鑰只放在 `.env`，不要提交至 Git。

完整設定、指令同步與疑難排解請看 [設定與部署](config/README.md)。RPG 的玩家規則與目前數值請看 [安安大冒險](config/RPG.md)。

## 專案結構

| 路徑 | 用途 |
| --- | --- |
| `ourbot.py` | 啟動、設定驗證、Cog 載入及斜線指令同步 |
| `cmds/` | Discord 事件與斜線指令入口 |
| `core/` | AI 記憶、語音及 RPG 的領域邏輯與互動介面 |
| `config/` | 可提交的行為設定、提示詞與現行玩家規則 |
| `docs/` | 功能規格、設計提案與平衡報告；分類見 [文件索引](docs/README.md) |
| `scripts/` | 平衡模擬、校準與遷移檢查工具 |
| `tests/` | 單元與整合測試 |
| `data/` | 執行期 SQLite 資料與狀態，不是設計文件 |
| `old_cmds/` | 已停用的舊指令，僅供歷史參考 |

## 驗證

```powershell
.\.venv\Scripts\python.exe -m pytest
```

文件描述與程式不一致時，以 `core/`、`cmds/`、`config/settings.toml` 及通過的測試為現況依據；平衡提案與歷史報告不會自動代表正式數值。
