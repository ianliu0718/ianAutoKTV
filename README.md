# ianAutoKTV

以 Python、Flask、Socket.IO、FFmpeg 與 Spleeter 建置的 KTV 系統，包含本機／區域網路版與 Render 雲端房間版，支援歌曲製作、播放、遙控、字幕，以及 QNAP NAS 和 Google Drive 歌庫。

## 維護原則

- 開始修改前先閱讀本文件及相關程式／測試；程式與測試是實際行為的依據。
- 以最小變更維持本機版與雲端版既有行為；架構或核心規則變更時同步更新本文件。
- [ToDo.md](./ToDo.md) 記錄尚待驗收的項目；未實際驗證的功能不得標示為已完成。
- 不提交 OAuth 憑證、權杖、部署設定、歌曲媒體或其他機密資料。

## 架構與入口

```text
YouTube
  -> yt-dlp 下載
  -> FFmpeg 轉碼
  -> Spleeter 分離人聲與伴奏
  -> FFmpeg 封裝六聲道 5.1 MP4
  -> FFprobe／完整解碼驗證
  -> 驗證成功後原子移入歌曲庫
  -> 播放器以 Web Audio 分流播放
```

| 檔案／目錄 | 職責 |
| --- | --- |
| `main.py` | 本機版主程式；共用 Flask、播放控制、音訊／字幕處理及歌曲製作流程。 |
| `render_app.py` | Render WSGI 入口；建立房間、隔離房間狀態並註冊雲端路由。 |
| `qnap_share_cloud.py` | QNAP 歌曲索引、同步狀態、快取及下載。 |
| `google_drive_cloud.py` | Render 房間成員的 Google Drive OAuth、歌庫及歌曲下載。 |
| `google_drive_prototype.py`、`google_drive_account.py` | 本機 Google Drive 功能及帳號資料處理。 |
| `GAS雲端歌單.gs` | NAS 索引快照的 Google Apps Script 端點。 |
| `templates/`、`static/` | 網頁、前端程式與靜態資源。`/combo` 以 iframe 共用 `/remote`。 |
| `tests/` | Python `unittest` 測試。 |
| `render.yaml`、`requirements-render.txt` | Render Blueprint、啟動命令及雲端依賴。 |
| `RENDER_DEPLOY.md` | Render 部署、環境變數與 Google Drive 設定說明。 |

## 歌曲來源與房間

- 本機版以 `ktv_songs` 等本機歌曲為來源；本機 Google Drive 歌曲下載後沿用既有歌曲播放路徑。
- Render 房間各自隔離播放佇列、KEY、音訊模式、字幕設定及 Google Drive 使用者狀態；NAS 公共快取由房間共用。Render 重啟會清除記憶體房間狀態及暫存資料。
- QNAP NAS 歌曲併入一般點歌清單；公共快取中的歌曲在所有房間都應顯示已下載狀態。Google Drive 歌曲則依房間成員的個人歌庫下載至房間快取。
- 歌曲來源標示：`☁️` 為雲端未下載、`☁️` 加「已下載」為雲端已下載、`💻` 為本機歌曲。已下載標籤不應把雲端歌曲誤標成本機來源。
- 本機／區網版與 Render 雲端版的 `/player`、`/combo` 在歌曲沒有字幕時，都會自動搜尋 LRCLIB、NetEase 的同步歌詞；找到相符歌詞後轉成歌曲對應的 `.vtt` 保存並同步字幕狀態，搜尋失敗或無匹配結果會顯示提示。雲端歌詞目前已由使用者確認可正確讀取。

## 核心不變條件

### 音訊

- 成品為六聲道 `5.1` MP4：`c0/c1` 原聲、`c2` 人聲、`c4` 伴奏；`c3/c5` 暫不使用。
- Spleeter 使用 `spleeter:2stems` 產生人聲及伴奏。需維持單次 AAC 合成、完整解碼與聲道布局驗證；驗證成功前不得發布半成品。
- 導唱以 `c2` 與 `c4` 混合，預設人聲增益 50%；伴奏模式只輸出 `c4`。
- 一般目標：`c2`／`c4` 約 `-14 LUFS ±1 LU`；`c4` True Peak 不高於 `-1 dBTP`、LRA 不高於 `8 LU`。可量測超標項目可寫入 `ktv_audio_exceptions`，但不能豁免聲道、完整性或量測失敗。

### 播放與字幕

- `playlist_queue[0]` 是目前歌曲；播放端與遙控端以伺服器廣播為準，不自行推測下一首。
- 切歌、正常播完與重唱是不同流程；升降 KEY、佇列推進及播放事件需維持既有行為，不可因重唱重設 KEY。
- `/combo` 遙控介面沿用 `/remote`；避免維護兩套不同實作。
- 字幕依 WebVTT cue 顯示；若無字幕，保留自動搜尋及明確狀態提示，不可讓歌曲無字幕且沒有任何訊息。

## Render NAS 同步：已確認的修正

- 根因：NAS warm-up 曾在 Gunicorn 載入 WSGI 時、worker fork 前啟動，造成背景同步執行緒與 worker 狀態分離。
- 修正：雲端路由註冊時不啟動 warm-up；由 worker 啟動 hook 及首個請求備援啟動同步。本機模式仍保留啟動預熱。
- NAS 歌庫 API 在背景載入期間回報載入狀態；檢查時以 API JSON 的同步狀態與歌曲頁為準。Access log 的 HTTP 狀態碼後數字是回應位元組數，不是歌曲數。
- Render 雲端索引／同步狀態使用暫存環境；服務重啟後可能須從 GAS 快照恢復。Render 部署設定與環境變數以 `render.yaml` 及 [RENDER_DEPLOY.md](./RENDER_DEPLOY.md) 為準，實際啟動命令以 Render 部署記錄確認。

## 開發環境

使用專案既有 Windows Python 虛擬環境與本機所需的 FFmpeg、Spleeter。於專案根目錄啟動本機／區域網路版：

```powershell
cd "D:\Buff\Cursor資料夾\OpenKTV-ianAuto360"
.\.venv\Scripts\Activate.ps1
python main.py
```

本機網址使用 HTTPS；主要頁面為 `/player`、`/remote`、`/admin` 與 `/combo`。首次使用裝置需接受本機 HTTPS 憑證。

本機測試 Render 房間版及 NAS 歌庫：

```powershell
cd "D:\Buff\Cursor資料夾\OpenKTV-ianAuto360"
$env:QNAP_SHARE_URL = "<QNAP 分享連結>"
$env:QNAP_SHARE_PASSWORD = "<QNAP 分享密碼>"
.\.venv\Scripts\python.exe render_app.py
```

啟動後開啟 `http://127.0.0.1:5000`。不要將真實密碼或其他機密值提交至版本庫。

執行測試：

```powershell
python -m unittest discover -s tests
```

## 打包與更新

在專案根目錄執行：

```powershell
# 建立完整發佈包
.\build_release.ps1

# 建立完整更新包
.\build_update.ps1

# 僅修改 templates 或 static 時建立前端更新包
.\build_update.ps1 -FrontendOnly
```

完整更新需一併更新執行檔及其相依檔案；不要覆蓋使用者的 `ktv_songs` 或模型資料。修改 `main.py`、FFmpeg／模型流程或打包設定時，不可只替換前端檔案。

雲端入口為 `render_app:application`，Render 依 `render.yaml` 安裝 `requirements-render.txt` 並啟動；部署及機密環境變數設定請參閱 [RENDER_DEPLOY.md](./RENDER_DEPLOY.md)。

## 後續交接

優先閱讀相關模組及 `tests/` 中對應測試，再依 [ToDo.md](./ToDo.md) 確認未完成驗收。排查雲端問題時，保留部署 commit、instance、同步程序與請求程序等診斷資訊；不要把舊事故時間線或單一部署的即時數值當成目前狀態。
