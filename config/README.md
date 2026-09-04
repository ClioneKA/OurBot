# 設定維護

本機 `.env` 保存密鑰與部署資訊，`config/settings.toml` 保存機器人行為。兩者修改後都需要重啟機器人。

| 檔案／區塊 | 內容 |
| --- | --- |
| `.env` | Discord Token、OpenAI／MiniMax API Key、FFmpeg／資料庫路徑、伺服器／頻道 ID、限頻豁免使用者 ID |
| `settings.toml` 的 `[ai]` | 一般模型、回覆機率、聊天歷史與輸入輸出長度 |
| `[ai.search]` | 搜尋開關、模型與每日額度 |
| `[ai.limits]` | 個人／頻道冷卻、整體限頻、每日額度與重置時區 |
| `[ai.memory]` | 印象摘要與好感度設定；`summary_model` 留空時使用 `ai.model` |
| `[ai.media]` | 圖片／語音回覆開關、長度、冷卻與額度 |
| `[tts]` | MiniMax 語音模型與音色 ID |
| `[rpg]` | RPG 等級開關、文字 XP／冷卻／長度、語音 XP／人數；詳見 [RPG 說明](RPG.md) |
| `[rpg.raid]` | 討伐活動、AI 怪物、隨機間隔、人數與獎勵；專用頻道在 `.env` 的 `RPG_RAID_CHANNEL_IDS` 設定 |
| `persona.txt`、`prompt_*.txt` | 人格與情境提示詞 |

初次部署時複製 `.env.example` 為 `.env`，填入密鑰與部署資訊，再調整 `settings.toml`。請勿把密鑰填入 TOML；TOML 與提示詞會納入 Git，`.env` 不會。

行為設定只讀 TOML，不再讀取舊的 `AI_REPLY_CHANCE`、`OPENAI_MODEL`、`MINIMAX_TTS_MODEL` 等環境變數。其他部署環境更新程式前，請先把自訂行為值轉移到 TOML。部署資訊仍沿用 dotenv 的規則：既有程序環境變數優先於 `.env`。

`core/settings.py` 集中定義型別、預設值與有效範圍。啟動時會檢查 TOML 語法、未知欄位、型別與數值範圍；設定錯誤會阻止啟動並指出欄位。省略欄位時採用程式預設值，整份檔案遺失則會報錯。設定檔路徑以專案位置為準。

程式透過 `get_settings()` 取得唯讀設定，例如：

```python
from core.settings import get_settings

settings = get_settings()
daily_limit = settings.ai.limits.user_daily_limit
```

Python 3.11 以上使用內建 `tomllib`，較舊版本由依賴清單安裝 `tomli`。
