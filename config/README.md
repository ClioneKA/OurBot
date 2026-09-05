# 設定維護

## 啟動與斜線指令

安裝 `requirements.txt` 的依賴，依 `.env.example` 填好 `.env` 後，在專案根目錄執行：

```powershell
.\ourbot\Scripts\python.exe -m pip install -r requirements.txt
.\ourbot\Scripts\python.exe .\ourbot.py
```

若虛擬環境無法建立 Python 程序，請先安裝 Python 3.12 以上，使用 `py -3.12 -m venv .venv` 建立新環境，並把上述命令的 `ourbot` 改成 `.venv`。虛擬環境依賴原本安裝的 Python，不能只複製資料夾使用。

啟動時先驗證設定，再於 `setup_hook` 載入全部 `cmds/*.py` 並同步應用程式指令；失敗會記錄原因並結束程序。重新連線不會再次同步。終端機與根目錄 `log.txt` 都會顯示模組名稱、同步範圍、Discord 回傳的指令名稱與 ID，以及登入後的 Application ID。

- `.env` 的 `DISCORD_SYNC_GUILD_IDS` 未設定或留空：透過 API 取得 bot 所在的全部伺服器，先清除這個 bot 的伺服器專用指令，再同步全域指令。沒有舊指令的伺服器會略過清除。
- 填入目標伺服器 ID（多個以逗號分隔）：將本機全域指令複製並同步至這些伺服器，方便快速測試；不更新全域指令。
- `AI_GUILD_IDS` 只控制 AI 回覆範圍，不控制斜線指令註冊。
- 全域模式下，伺服器清理失敗會停止啟動；已完成的清理不會回復，修正錯誤後重啟即可重試。全域同步若在清理後失敗，既有全域指令仍保留，新的版本需重啟重試。
- 指定伺服器模式仍會保留既有全域指令；切回全域模式時會自動清除伺服器版本。程序環境變數優先於 `.env`，請同步檢查部署環境。

排查時先確認 `log.txt` 出現「指令同步成功」。若只有模組載入紀錄，查看後續錯誤；若同步成功但 Discord 沒顯示，確認登入的 Application ID 是預期的 bot、邀請授權包含 `bot` 和 `applications.commands`，並檢查伺服器整合設定、使用者與頻道的應用程式指令權限，再重新載入 Discord。全域指令更新可能有客戶端快取，可使用指定伺服器同步排查。

指令明確使用伺服器安裝範圍（`integration_types=[0]`，需要 discord.py 2.4 以上）。全域同步後另以 GET 讀回清單，記錄每個指令的安裝範圍、使用情境與預設權限。`contexts` 包含 `0` 表示可在伺服器頻道使用；未指定時採用 API 預設。`guild_only` 仍可用於全域指令，它限制使用場合，不代表伺服器專用註冊。若 GET 清單完整且範圍正確，請比較另一個 Discord 客戶端與頻道內 `/` 選單，區分整合頁顯示問題與實際不可使用。遠端指令 ID 不一致可能代表另一個程序使用相同 token 覆寫了指令。

此 bot 使用全部 intents，請在 Discord Developer Portal 的 Bot 設定啟用所需的 Privileged Gateway Intents；未授權時 Gateway 連線會失敗，即使 REST 指令同步成功也無法正常回應。

遠端查核直接記錄 HTTP 回傳的原始 JSON 欄位，沿用 discord.py HTTP client 的驗證與限頻處理。本機 discord.py 2.7.1 可重現將 `[0]` 安裝／情境欄位解析成 `[]` 的問題，且 `AppCommand.to_dict()` 不包含預設成員權限，因此不要使用該轉換結果判斷 Discord 上是否允許伺服器使用。新版紀錄前綴為「遠端原始指令」。

## 行為設定

本機 `.env` 保存密鑰與部署資訊，`config/settings.toml` 保存機器人行為。兩者修改後都需要重啟機器人。

| 檔案／區塊 | 內容 |
| --- | --- |
| `.env` | Discord Token、OpenAI／MiniMax API Key、FFmpeg／資料庫路徑、伺服器／頻道 ID、限頻豁免使用者 ID |
| `settings.toml` 的 `[ai]` | 一般模型、回覆機率、聊天歷史與輸入輸出長度 |
| `[ai.search]` | 搜尋開關、模型與每日額度 |
| `[ai.limits]` | 個人／頻道冷卻、整體限頻、每日額度與重置時區 |
| `[ai.memory]` | 印象、好感度及伺服器共同記憶設定；`summary_model` 留空時使用 `ai.model` |
| `[ai.media]` | 圖片／語音回覆開關、長度、冷卻與額度 |
| `[tts]` | MiniMax 語音模型與音色 ID |
| `[rpg]` | RPG 等級開關、文字 XP／冷卻／長度、語音 XP／人數；詳見 [RPG 說明](RPG.md) |
| `[rpg.raid]` | 討伐活動、AI 怪物、一般 30–60 分鐘隨機間隔、台灣時間 12:00–14:00／18:00–23:00 減半為 15–30 分鐘、人數與獎勵；專用頻道在 `.env` 的 `RPG_RAID_CHANNEL_IDS` 設定 |
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
