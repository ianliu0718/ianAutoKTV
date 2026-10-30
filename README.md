# ianAutoKTV

ianAutoKTV 是以 Python、Flask、SocketIO、FFmpeg 與 Spleeter 建立的 KTV 系統，同時支援 **本機／區域網路版（`main.py`）** 與 **Render 雲端房間版（`render_app.py`）**，提供 YouTube 歌曲製作、播放、遙控、批量新增、既有歌曲維護，以及 QNAP NAS 與 Google Drive 雲端歌庫整合功能。

## README 規則與修改確認

- 本文件是本專案架構、音訊規則與製作流程的依據；新對話開始程式工作前，先閱讀相關段落。
- 若工作需要修改本文件，先說明預計修改內容，並使用 VS Code 選擇提示提供「先看差異」、「修改」、「取消」選項；收到選擇前不得編輯。
- 若選擇「先看差異」，先提出具體差異，不套用修改，再取得明確同意；選擇「修改」只核准已說明的內容，選擇「取消」則保持本文件不變。
- 若需求與本文件的既有規則衝突，或需要變更規則，先停下並詢問，不得自行改動規則或降低驗收標準。

## Google Drive 前置設定與驗收順序

依下列順序逐步完成；未通過某一步時先記錄錯誤與證據，不跳過驗收，也不擴大 OAuth 權限。Google Cloud Console 的選單名稱可能隨介面更新調整，請以官方文件為準。

1. **使用正式專案與準備驗收資料**：使用已建立的正式 `ianAutoKTV` 專案，記錄 Project ID 與 Project number。以該 KTV 安裝預計共用的 Google 帳號在 Drive 建立專用歌庫資料夾，放入一首可合法驗收的六聲道 MP4、一個子資料夾及一個非支援檔案，供確認列檔範圍；不另建測試 Cloud 專案，但正式上線前仍須逐項完成本文件及 ToDo 的功能驗收。
2. **啟用 API 與建立 API key**：在 API Library 分別啟用 **Google Drive API** 與 **Google Picker API**。接著到「API 和服務」>「憑證」建立 API key，並依 [Picker 網頁整合文件](https://developers.google.com/workspace/drive/picker/guides/web-picker)限制允許的網站來源及可用 API。
3. **設定 OAuth 同意與資料存取範圍**：在 Google Auth Platform 的 Branding、Audience、Data Access 設定應用資訊、支援聯絡方式與實際使用對象；依正式發布及 Google 驗證要求完成設定，不另以測試專案取代正式專案。Drive 權限優先使用較窄的 `drive.file`；但此 scope 實測無法列出本歌庫中已存在且未逐檔授權的歌曲。經使用者同意，本原型改用 `drive.readonly` 驗證自動列出及讀取既有歌曲。Google 將此 scope 列為受限範圍，授權可讀取及下載整個 Drive；正式使用前必須確認並完成適用的 OAuth 驗證與安全評估要求。參考 [OAuth 同意畫面設定](https://developers.google.com/workspace/guides/configure-oauth-consent)。
4. **建立 Desktop OAuth client**：建立 Desktop app 類型的 OAuth client，採用系統瀏覽器與 PKCE/loopback 回呼，由 KTV 主機本機啟動授權。這個流程不把各家庭不同的區網 IP 設為 OAuth 網頁來源，也不要求把 KTV 對外開放；實際流程仍須依 ToDo 驗證。參考 [Google 桌面應用 OAuth](https://developers.google.com/identity/protocols/oauth2/native-app)。
5. **限制 Picker API key 並保護權杖**：依 [Picker 網頁整合文件](https://developers.google.com/workspace/drive/picker/guides/web-picker)限制 API key 可用 API 與來源，包含 KTV 主機本機 Picker 頁面（目前伺服器 port 為 `5000`，來源依實際頁面設定）及 `https://docs.google.com/*`。Desktop OAuth client 是公開用戶端，Client Secret 不可視為可靠的保密邊界；雖然 Google 文件將其列為選填，若實際權杖交換要求提供，允許由 KTV 部署端設定並由伺服器端使用，不要求終端使用者輸入。Client Secret 不得提交至版本庫、寫入一般日誌或交給區網瀏覽器。refresh token 只保存在 KTV 主機的安全儲存區，不得提交至版本庫、寫入一般日誌或交給區網瀏覽器。Picker 必要的短效 access token 可暫時交給 KTV 主機本機頁面，並允許 Picker SDK 在 Picker 開啟期間將其放入 `docs.google.com` iframe URL；這是唯一 URL 例外，不得放入 KTV 頁面自身網址或其他連結，不得寫入 localStorage、sessionStorage、Cookie 或日誌，Picker 關閉或完成後須清除頁面與伺服器端暫存。分享頁面、截圖或除錯資料前，必須確認不含該 iframe URL 或權杖。
6. **驗證 Picker 與資料夾列檔**：由 KTV 管理者在主機本機頁面完成授權並透過 Picker 選取歌庫資料夾。本原型目前使用 `drive.readonly`；確認只保存資料夾 ID 與必要 metadata，並由後端依所選資料夾的 Drive 類型設定 `user` 或 Shared Drive `drive` corpus，使用 `'<folderId>' in parents` 和 `trashed = false` 列出直接子項目、逐頁讀取並排除資料夾及不支援格式。確認子資料夾內歌曲不出現，且既有歌曲可列出。雖然 OAuth scope 本身可讀取整個 Drive，正式下載實作必須先確認檔案是所選歌庫第一層的有效項目，不得接受任意 file ID。若 API 回傳空清單，記錄資料夾 ID 核對、查詢 corpus、分頁數及結果，停止驗收並排查原因；不得把空清單視為成功。
7. **驗證後台完整下載**：以驗收 MP4 的原始 Drive API 媒體內容驗證後台完整下載，不使用預覽頁或轉碼網址。確認下載完成後的位元組數與 Drive metadata 大小相符、檔案可完整解碼，並保存於 KTV 主機專用歌曲目錄，直到管理者透過清除管理刪除。測試中斷、磁碟空間不足及 Drive API `401/403/404/429` 等失敗時，不得留下可被當成完整歌曲播放的部分檔案；不得在命令列歷史、截圖或回報中包含 `Authorization` header 或權杖。
8. **驗證本機播放、六聲道與錯誤恢復**：在預定支援的桌面、手機及電視瀏覽器，以已下載至 KTV 主機的歌曲實測起播、暫停、seek、續播與切歌；確認本機播放來源可由 Web Audio 讀取六聲道，原聲／導唱／伴奏、KEY 與字幕均正常。確認管理者可檢視並清除已下載歌曲，再測試權限撤銷、權杖過期、Drive 原始檔刪除、離線與 API 錯誤，並確認既有下載檔會保留至管理者清除；確保下載及播放失敗可理解、可恢復或可重試。
9. **保存驗收紀錄**：記錄驗收日期、瀏覽器／裝置、OAuth scope、API 回應狀態與必要 headers、功能結果及錯誤處理結果；遮蔽 Client Secret、權杖、API key 與歌曲內容。只有實際通過的 ToDo 項目才可勾選。

官方參考：[Picker 網頁整合](https://developers.google.com/workspace/drive/picker/guides/web-picker)、[Picker OAuth token 設定](https://developers.google.com/workspace/drive/picker/reference/picker.pickerbuilder.setoauthtoken)、[Drive API OAuth 範圍](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)、[桌面應用 OAuth](https://developers.google.com/identity/protocols/oauth2/native-app)、[Drive API 限流](https://developers.google.com/workspace/drive/api/guides/limits)、[Drive API 錯誤處理](https://developers.google.com/workspace/drive/api/guides/handle-errors)。

## 系統架構

```text
YouTube URL
  -> yt-dlp 下載
  -> FFmpeg 轉為 H.264 / 720p / 30fps
  -> Spleeter 2-stems 直接產生 vocals.wav 與 accompaniment.wav（舊 4-stems 目錄則將 drums/bass/other 合成 accompaniment.wav）
  -> FFmpeg 建立六聲道 5.1 MP4
  -> FFprobe 與完整解碼驗證
  -> 原子搬入 ktv_songs
  -> player/combo 以 Web Audio 分流播放
```

### 1. 本機／區域網路版 Google Drive 歌曲下載與播放來源

每台 KTV 安裝共用一個 Google 帳號與歌庫，並與本機 `ktv_songs` 路徑並存：

> **目前實作狀態：**`main.py`（在非雲端模式下透過 `google_drive_prototype.py`）註冊本機 OAuth／Picker／資料夾列檔與單曲下載。KTV 管理者可在主機本機頁連結 Google 帳號；OAuth Client ID、必要時的 Client Secret 與 Picker API key 由安裝目錄的 `google_drive_deploy.json` 提供，終端使用者不必輸入用戶端設定。授權改為 Desktop OAuth PKCE/loopback 離線存取，refresh token 以 Windows DPAPI 加密保存於該安裝的 `google_drive_account.bin`，資料夾 ID／名稱在第一層列檔成功後一併保存。短效 access token 仍只暫存於主機本機頁面與 Picker；區網可查看共用帳號與資料夾，但不能變更授權。下載仍僅能在短效授權流程內操作；伺服器會重新確認歌曲仍是所選資料夾的直接子項目，並將 Drive 原始媒體串流至 `drive_downloads/`，檢查檔案大小後以 Drive file ID 雜湊命名並原子保存，同時以 `drive_downloads.json` 原子保存 file ID 與本機檔名、來源名稱、資料夾、大小及修改時間的對應。Drive metadata 與原始媒體下載已加入有限退避重試：429／限流型 403 最多 5 次，網路／5xx 最多 3 次；401、權限型 403、404 與磁碟／完整性錯誤會明確失敗並清除半成品。`/api/list` 會合併具有完整 metadata 的已下載 Drive 歌曲，`/songs/<filename>` 會安全解析至對應本機檔案，讓 `/remote`、`/combo` 與播放事件沿用既有 filename-based 流程。KTV 主機本機管理頁已提供下載歌曲、來源 file ID、檔案大小、磁碟用量及單首／全部清除；管理 API 不開放區網遙控器。

```text
KTV 管理者在伺服器端啟動帳號連結，由 KTV 主機開啟系統瀏覽器進行 Desktop OAuth PKCE/loopback 授權
  -> KTV 伺服器使用部署設定中的 OAuth Client ID（及必要時的 Client Secret）交換授權碼；終端使用者只登入自己的 Google 帳號
  -> KTV 主機安全保存該安裝唯一帳號的 refresh token
  -> Picker 暫時取得短效 access token（只在主機本機頁面記憶體）
  -> 管理者選定一個 Drive 資料夾，KTV 後端只列出該資料夾第一層歌曲
  -> 區網遙控器共用歌庫；播放佇列保存來源類型與 Drive file ID
  -> 歌曲被指定下載時，KTV 後台確認檔案屬於所選資料夾第一層，再完整下載 Drive 原始 MP4 至專用本機歌曲目錄
  -> 完整檔案保留至管理者透過清除管理刪除
  -> player/combo 從本機下載檔以 Web Audio 分流播放
```

同一台 KTV 的所有區網遙控器依產品設計共用已連結帳號及歌庫，不提供遙控使用者層級的 Google 帳號隔離。登入與資料夾選擇由 KTV 管理者在伺服器端操作；終端使用者不需自行輸入 OAuth 用戶端設定。refresh token 僅保存在 KTV 主機；Picker 必要的短效 access token 只暫時交給主機本機頁面，不交給區網遙控器。僅在 Picker SDK 必須使用時，允許短效 access token 暫時出現在 `docs.google.com` Picker iframe URL；不得出現在 KTV 頁面自身網址、應用程式日誌或一般儲存空間，且 Picker 關閉或完成後須清除頁面與伺服器端暫存。Desktop OAuth client 是公開用戶端，Client Secret 不得視為可靠的保密邊界；若部署需要使用，僅由伺服器端設定與傳送，且不得提交至版本庫、寫入一般日誌或交給區網瀏覽器。後台只可下載所選資料夾第一層中已確認的有效檔案；下載檔保存於 KTV 主機專用目錄，直到管理者透過清除管理刪除。本機 `ktv_songs` 與既有 `/songs/<filename>` 播放路徑維持不變。

### 2. Render 雲端房間版架構與雙歌庫設計（`IANAUTOKTV_CLOUD_MODE=1`）

雲端版以 `render_app.py` 作為入口（設定 `IANAUTOKTV_CLOUD_MODE=1`），採用多房間架構與「預設 QNAP NAS 房間歌庫 + Google Drive 個人歌庫」雙歌庫設計：

- **房間建立與狀態隔離（`render_app.py` + `main.py`）**：
  - 首頁（`/` 或 `/rooms`）提供「建立房間」（`POST /api/rooms/create`，產生 8 位數房號與房主 token，導向 `/combo`）與「加入房間」（`POST /api/rooms/join` 或掃描 QR Code `GET /join/<code>`，導向 `/remote`）。
  - 每個房間透過 `contextvars`（`_CURRENT_CLOUD_ROOM`）隔離各自的待播清單（`playlist_queue`）、播放進度、升降 KEY、音訊模式（原聲／導唱／伴奏）、字幕設定及房間專屬快取目錄 `drive_downloads/rooms/<room_code>`。
  - 閒置且無連線的房間逾時後會自動清理並刪除房間快取目錄。
  - 雲端模式關閉 `/admin`、影片轉檔、字幕上傳、改名與本機限定 `/api/drive-prototype/*` 等管理端點。
- **預設 QNAP NAS 房間歌庫（`qnap_share_cloud.py` + `templates/remote.html`）**：
  - 透過環境變數 `QNAP_SHARE_URL`（或 `NAS_SHARE_URL`）與 `QNAP_SHARE_PASSWORD`（或 `NAS_SHARE_PASSWORD`）設定預設 QNAP myQNAPcloud 分享連結，支援 `https://www.myqnapcloud.com/smartshare/...`（透過 myQNAPcloud CloudLink 與 QNAP File Station `get_tree`、`get_list`、`get_file_list` 解析）與直接 `share.cgi?ssid=...` 連結。
  - 伺服器啟動時於背景預熱並快取預設 NAS 歌單（`_warm_env_nas_cache`），自動遞迴掃描分享資料夾內的 `.mp4` 歌曲並配對同名 `.srt`、`.lrc` 或 `.vtt` 字幕檔。
  - 使用者一進入遙控器（`/remote` 或 `/combo` 內嵌遙控區），前端自動呼叫 `GET /api/nas/library-songs`，將預設 NAS 歌曲直接整合顯示在主畫面的 **「🎶 點歌」面板**（`#song-panel` / `#list`），完整支援搜尋、分類篩選（歌曲字數、主唱者、語言、歸屬）、排序（新歌、熱門、冷門、國語、台語）與 `字幕` 標籤。
  - 點擊尚未快取的 NAS 歌曲（或點選「播歌」）時，按鈕顯示 `下載中...` 並呼叫 `POST /api/nas/library-songs/download`，後端串流下載 MP4 與配對字幕至房間快取目錄（保留原始 MP4 檔名以對齊歌名解析、播放狀態、點播次數與筆記），完成後自動送出 `add_to_queue` 加入待播清單。
- **Google Drive 個人歌庫（`google_drive_cloud.py` + `templates/remote.html`）**：
  - 遙控器左上角的 **`[☁ 雲端歌庫]`** 按鈕開啟專屬於 **Google Drive 個人歌庫** 的彈出視窗（`#drive-library-panel`）。
  - 使用 Web OAuth（環境變數 `GOOGLE_CLIENT_ID`、`GOOGLE_CLIENT_SECRET`、`GOOGLE_PICKER_API_KEY`、`GOOGLE_PROJECT_NUMBER`），以「房間內各別瀏覽器使用者」為單位隔離 `refresh_token` 與所選資料夾；房間成員可連結自己的 Google Drive 並透過 Google Picker 選擇個人歌庫資料夾，點歌時下載至房間共用快取目錄並排入房間待播清單。

單曲新增、批量新增與管理端進階 AI 去人聲，必須共用相同的音訊分類、聲道映射、響度處理與驗證規則。批量作業逐首執行，不得同時啟動多個 Spleeter 工作。

## 歌曲檔名與點歌分類

- 新歌曲檔名格式為 `歌名-主唱者-語言-歌曲代號-歸屬.mp4`；歌曲代號按數字解析並反向排序，不受歸屬後綴影響。
- 舊有四欄格式 `歌名-主唱者-語言-歌曲代號.mp4` 保持相容，歸屬分類為 `未標記`。
- `/remote` 與 `/combo` 點歌區依歌曲名稱、主唱者、語言與歸屬分類；歸屬支援多選，所選歸屬採 OR 比對，其餘分類與搜尋文字採 AND 組合。分類選取不會清除其他分類或搜尋文字。輸入非空搜尋文字或按下清除時會重設其他分類；若排序模式為國語或台語，語言選單仍保留對應的「國」或「台」，直到排序模式切離。產生各分類選項時會忽略該分類自身的選擇，並依搜尋文字和其餘分類條件，只列出仍有符合歌曲的候選值。
- 點歌搜尋列前的排序按鈕依序循環 `新歌 -> 熱門 -> 冷門 -> 國語 -> 台語`；`新歌` 依歌曲代號反向排序，`熱門` 依歌曲開始播放次數由高至低排序，`冷門` 依播放次數由低至高排序，兩者同次數都沿用歌曲代號反向順序。`國語` 與 `台語` 模式會自動將語言選單設為「國」與「台」，並套用熱門排序。歌曲開始播放時才增加一次計數，不因加入待播清單而計數；計數保存於專案根目錄的 `song_play_counts.json`，並同步給 `/remote` 與 `/combo`。

## 音訊規則

| 模式 | 內容 | 聲道 |
|---|---|---|
| 原聲 | 原始檔案的音軌；盡量以約 -14 LUFS 為基準，偏差可接受，不要求 -1 dBTP | c0/c1 |
| 導唱 | 播放時將 c2 人聲 stem（預設 50%）與 c4 正常音量伴奏混合；可調整人聲 0–100% | c2 人聲；c4 伴奏 |
| 伴奏 | 只有正常音量的伴奏 | c4；播放時複製至左右輸出 |
| 暫時不使用 | c3 |
| 暫時不使用 | c5 |

新進歌曲入庫前，原聲 c0/c1 盡量以約 `-14 LUFS` 為基準，但響度偏差可接受，不以此單獨阻擋入庫，也不要求 `-1 dBTP`；若完全無法量測仍視為製作驗證失敗。導唱 c2 與伴奏 c4 的一般響度目標均為 `-14 LUFS`，驗收容差為 `±1 LU`；c4 True Peak 一般不高於 `-1 dBTP`，LRA 不高於 `8 LU`。凡共用驗收流程中可量測、但超出其數值標準的項目，都可作為「特殊歌曲」收錄；不設超標數值上限，亦允許同一首歌同時標記多個超標項目。仍必須通過六聲道布局、媒體完整解碼及所有必要指標成功量測等結構性驗證；量測失敗或結構錯誤仍拒收。c0/c1 響度偏差原本即不作硬性拒收條件；c2+c4 導唱混音依使用者設定變化，不作固定響度/峰值驗收。特殊歌曲的 MP4 metadata 使用 `ktv_audio_exceptions` 記錄每個超標模式、指標、實測值及標準，metadata 以不重新編碼音訊的 remux 寫入。管理端、遙控端與整合端的選歌項目都顯示「特殊歌曲」超標類別與實測值，例如 `伴奏 c4 TP +2.4 dBTP（標準 ≤ -1 dBTP）`。舊版 c4 TP/LRA metadata 仍可讀取相容；新流程不再套用 `+0.5 dBTP` 或 `14 LU` 例外上限，也不限制 TP 與 LRA 同時標記。整體製作仍只執行一次 AAC 合成，不重新下載、分離或 AAC 重編碼。c2 儲存切割後的獨立人聲，c4 儲存正常音量伴奏。c4 為單聲道 stem，會先在 PCM 階段以 `-3 dBTP` 為峰值上限及 `7 LU` LRA 目標進行響度處理，補償平均音量時也不得超過此峰值上限，為 AAC 編碼保留峰值餘裕；此為合成前處理目標，不變更最終超標可標記收錄的規則。播放導唱時以預設 50% 人聲增益將 c2 與 c4 即時混合；不對 c2+c4 混音結果再做整體響度或峰值校正，且滑桿不改變伴奏音量或聲道來源。

### 伴奏製作規則

1. Spleeter 使用 `spleeter:2stems` 直接產生 `vocals.wav` 與 `accompaniment.wav`（避免 4-stems 合成伴奏時把原始人聲殘留混回伴奏；若讀取舊版 4-stems 目錄且缺少 `accompaniment.wav`，才由 FFmpeg 將 `drums.wav`、`bass.wav`、`other.wav` 合成 `accompaniment.wav`）。
2. 伴奏檔必須先通過 FFmpeg 完整解碼驗證。
3. 將伴奏左右聲道平均成單一正常音量伴奏訊號。
4. 同一個非人聲伴奏訊號供導唱播放混音與 c4 使用；新格式 c2 只存切割後的人聲，c4 只存 `accompaniment.wav`。c4 單聲道 stem 在唯一一次 AAC 合成前，以 PCM 響度處理將峰值控制在 `-3 dBTP` 以下並以 `7 LU` LRA 為目標，補償平均音量時不得突破峰值上限；AAC 完成後依雙聲道播放方式驗收 `-14 LUFS ±1 LU`、True Peak 不高於 `-1 dBTP`、LRA 不高於 `8 LU`。導唱 c2 的一般響度目標同為 `-14 LUFS`、驗收容差 `±1 LU`。凡共用流程實際驗收的可量測音訊指標超出標準，均以 `ktv_audio_exceptions` 記錄模式、指標、標準與實測值後作為特殊歌曲收錄；例外無數值上限，TP、LUFS、LRA 等多項超標可同時標記。聲道布局、完整解碼、必要指標成功量測仍為不可豁免的硬性驗收。新例外標記以不重新編碼音訊的 remux 保存，選歌介面顯示標記及實測值；舊版 c4 TP/LRA metadata 保持讀取相容。不得重新下載、分離或 AAC 重編碼。播放導唱時直接混合 c2 與 c4，不對混音套用額外整體增益或正規化；遙控端或整合端滑桿只改變 c2 增益。舊歌曲若無新版格式標記，仍依原 c2 混合導唱播放。
5. 不得對伴奏執行第二次 Spleeter、左右相減、Mid/Side 濾波或其他未驗證加工。
6. 不得反覆 AAC 重編碼；完成六聲道後只執行必要的最終響度處理。

## 專案架構流程規劃

本專案採用「單一來源、共享流程、嚴格驗證」的工程設計，確保新增曲目、批量製作與既有歌曲修正都遵循同一套音訊與播放規則。核心流程如下：

1. 需求與資料模型定義：確立歌曲分類（原聲/導唱/伴奏）、音訊規則、播放模式、待播隊列與遙控事件。
2. 單曲製作流程：驗證來源、下載、轉碼、分離、聲道組裝、響度校正、驗證與搬入 `ktv_songs`。
3. 批量製作流程：逐首複用單曲製作流程，並在失敗時收集紀錄，不允許混用不同處理規則。
4. 播放與控制層：Queue、random idle fill、手動點歌、切歌、重播、字幕、KEY、音量與畫面延遲調整。
5. 品質保證與自動修正：每首歌曲必須通過 `FFprobe`、`FFmpeg` 完整性與聲道布局驗證，且所有必要指標都必須成功量測；原聲 LUFS 作為約 `-14 LUFS` 的校正參考，偏差可接受；超出其他 LUFS、dBTP 或 LRA 門檻的項目記錄為特殊歌曲 metadata，不阻擋入庫。
6. 發布與維護：版本更新、前端更新、執行檔更新與必要檔案同步，確保使用者資料不被誤刪。

### 批量新增與進階功能一致性要求

- 批量新增 KTV 歌曲必須直接套用「新增 KTV 歌曲」的完整 pipeline。
- 進階所有功能（AI 去人聲、音訊平衡、效能最佳化、既有影片修正）都必須沿用同一套聲道映射與驗證規則。
- 禁止在批量流程中使用不同於單曲流程的臨時語音處理規則，以免造成同一類曲目訊號不一致。

## 製作流程

1. 驗證 YouTube URL 與歌曲名稱。
2. 使用 yt-dlp 下載最高 720p 的可用影音來源；失敗時清理半成品並嘗試下一個格式。
3. 使用 FFmpeg 轉換為 H.264、YUV420P、最高 1280x720、30fps，保留雙聲道音訊。
4. 使用 `spleeter:2stems` 直接分離出人聲（`vocals.wav`）與伴奏（`accompaniment.wav`）；若為舊 4-stems 輸出目錄則將非人聲三 stem 合成伴奏。
5. 依音訊規則建立 c0/c1、c2 人聲、c4 伴奏。
6. 以 `join/5.1` 封裝成單一 MP4。
7. 依各模式的音量基準處理並量測：原聲盡量接近 `-14 LUFS`，偏差可接受且不要求 `-1 dBTP`；導唱與伴奏的響度目標均為 `-14 LUFS`、驗收容差均為 `±1 LU`；伴奏另以 `7 LU` LRA 為處理目標，最終 True Peak 不高於 `-1 dBTP`、LRA 不高於 `8 LU`。共用驗收流程中已量測而超出門檻的項目均記錄於 `ktv_audio_exceptions`，可無上限地標記並收錄；必須成功量測且通過檔案完整性與六聲道驗證。
8. 使用 FFprobe 確認第一條音訊流為 6 channels、配置為 `5.1`，並使用 FFmpeg 完整解碼。
9. 所有驗證成功後才搬入 `ktv_songs`；失敗時不得留下可播放半成品。

## 播放架構

遙控器會依伺服器廣播的 `seek_correction` 狀態決定是否顯示畫面進度控制區；伺服器關閉時整個控制區隱藏，而不是只顯示停用按鈕。

`/combo` 的播放視窗沿用 `/player` 的播放畫面呈現規格；整合端遙控控制區與播放視窗分離，不改變播放畫面的呈現規則。

`/combo` 的遙控區以同源 iframe 載入 `/remote`，共用相同的點歌／實際遙控分頁、設定與互動；遙控介面變更時以 `/remote` 為單一來源，避免兩端功能與版面分岔。

`/remote` 與 `/combo` 依相同的 `seek_correction` 狀態互斥切換歌曲位置控制：單媒體模式只顯示 `[< 快退 10 秒]` 與 `[快進 10 秒 >]`，透過 `seek_song` 將目前歌曲位置調整 `-10` 或 `+10` 秒；雙媒體模式隱藏歌曲快退／快進，改顯示五個畫面進度控制按鈕，透過 `seek_video` 調整畫面與音訊的相對位置，按鈕由左至右送出 `+0.5`、`+0.1`、`0`、`-0.1`、`-0.5` 秒。伺服器在 `seek_correction` 啟用或待播清單為空時拒絕 `seek_song`；播放器只處理目前播放檔案的跳轉，雙媒體校正則維持音訊時間與畫面進度偏移。

`/remote` 提供預設開啟的「螢幕保持常亮」開關，使用瀏覽器 Screen Wake Lock API 防止手機或平板因閒置自動關閉螢幕。`/combo` 的遙控區嵌入相同的 `/remote` 頁面，因此提供相同開關與行為。設定會保存在該裝置的瀏覽器；頁面回到前景時會重新取得鎖定。使用者手動關閉後會保存關閉狀態。若瀏覽器不支援 Wake Lock，開關會自動停用，不能取代作業系統本身的省電或螢幕鎖定政策。

`/remote` 的「實際遙控」分頁以右上角「設定」面板收納「顯示播放端 QR Code」、「無點歌時隨機播歌」與「螢幕保持常亮」三個開關；新增會依條件隱藏／顯示的面板時，沿用 `.toggle-panel` 綠色外框樣式。

- 原聲：`c0,c1`
- 導唱：`c2`，播放時複製至左右輸出；新式獨立人聲軌（`handler_name=vocal-stem-v1`）則以預設 50% 人聲增益混合 `c2` 與 `c4`
- 伴奏：`c4`，播放時複製至左右輸出

播放器在 `loadedmetadata` 確認實際聲道數後，才建立 Web Audio 音訊圖；因此不會以瀏覽器預設雙聲道誤判六聲道歌曲。預設情況下，伺服器關閉「畫面進度調整」時，播放器使用單一 `<video>` 元素同時負責畫面、音訊、字幕與 Web Audio 來源，避免低效能電腦同時解碼兩個媒體元素。

`/remote` 與 `/combo` 的字幕按鈕會循環切換五種顯示模式：`字幕：隱藏`、`字幕：一般`、`字幕：單行`、`字幕：雙行`、`字幕：雙行漸變`。一般模式以目前 cue 為起點顯示約 12 行歌詞並平順向上移動；單行、雙行與雙行漸變則依各 cue 時間顯示動態字幕。字幕樣式按鈕另提供兩種模式：`背景` 與 `陰影`；兩者都會同步套用到 `/player` 與 `/combo`，預設為背景。

`/remote` 新增會依播放狀態或設定條件隱藏／顯示的控制面板時，必須套用 `.toggle-panel` 共用樣式，以 `#2aa894` 綠色外框與一致的深綠背景呈現；切換顯示狀態不得移除此外框。

### 動態歌詞顯示規則

- 動態歌詞以 WebVTT cue 的時間軸為準；歌詞開始前的前導空白不計入歌詞順序。
- 兩個有歌詞 cue 之間的時間空檔會建立一個空白 slot，等同於該時間段的歌詞是空字串 `""`，並且會計入後續上下行排列順序。
- `字幕：一般` 以目前 cue 為起點，顯示約 12 行歌詞並依 cue 時間平順向上移動；沒有 active cue 時清空字幕內容。
- `字幕：單行` 只顯示目前動態歌詞並置中，不會提前顯示尚未開始的下一句。
- `字幕：雙行` 以 KTV 方式排列目前 cue 與下一個 slot；空白 slot 也保留行的位置與高度。當前句序列會依 slot 順序交替排列，例如：

  ```text
  [第一句] / [第二句]
  [第三句] / [第二句]
  [第三句] / [第四句]
  [第五句] / [空白]
  ```

- 若目前句前面是空白 slot，會保留第一行空間；若目前句後面是空白 slot，會保留第二行空間。空白行的高度與正常歌詞行相同，不會造成字幕上下跳動。
- 雙行排列時，第一行靠左、第二行靠右；兩行的內側邊界靠近播放畫面中央。長文字可向外延伸，但不會因固定字數限制而換行或裁切。
- 字幕寬度以實際播放畫面像素寬度計算，不使用固定中文字數限制。當單行文字超過可用寬度時，會自動縮小該行字體，保持單行、不換行、不裁切；視窗縮放或字幕比例變更時會重新計算。
- `字幕：雙行漸變` 使用目前 cue 的開始與結束時間計算遮罩進度，以 `requestAnimationFrame` 平滑更新高亮位置；目前正在演唱的行由左至右逐字顯現，下一行維持未高亮狀態。
- 背景模式的漸變高亮層會與文字 padding 對齊，陰影模式則維持相同文字座標；背景、陰影、描邊與漸變不會改變歌詞時間軸排列。

播放畫面的播放清單狀態列會依實際可用寬度盡量顯示歌曲；只有下一首歌曲再加入後會超出畫面時，才在最後顯示 `...`，不再固定只顯示前三首。

當伺服器開啟「畫面進度調整」時，才會切回雙媒體元素控制模式，讓遙控器可調整畫面相對音訊的進度；雙媒體模式下，視覺用的 `<video>` 必須被靜音，而實際音訊輸出只能由 Web Audio 的 `<audio>` 來源傳出，否則同一首歌會被同時播放兩次，造成原聲 / 導唱 / 伴奏互相重疊且模式切換看起來失效。當調整關閉時，會清除偏移並同步所有播放端，遙控器按鈕會停用，並回到單一 `<video>` 進行播放。

畫面進度調整使用伺服器累積的有號 offset：正值表示畫面落後音訊，負值表示畫面超前音訊，`0` 表示同步；播放端依 `videoTime = audioTime - offset` 對齊兩條時間軸。

播放端停止歌曲或切換播放模式時，會先以短時間淡出與平滑增益切換，再清空媒體來源，避免大音量喇叭產生 click/pop。

### 情緒價值與音效

遙控器與 `/combo` 的播放控制區提供 `[情緒價值]` 面板。面板以分頁管理不同效果，避免未來增加功能後內容過度擁擠：

- `[彈幕]` 分頁保留原本的即時彈幕功能，可送出預設鼓勵、自訂文字或隨機誇獎。
- `[音效]` 分頁提供 `[拍手音效]`，送出 Socket.IO 的 `sound_effect` 事件，目前支援 `clap`。
- `[貼圖]` 分頁從專案 `static/stickers/` 載入核准的 GIF、動畫 WebP、APNG、PNG 素材；點擊貼圖即直接送出，每個遙控器 2 秒內最多傳送一張。伺服器只廣播 allowlist 中的檔名，`/player` 與 `/combo` 直接顯示原始貼圖並於約 5 秒後移除，保留動畫與透明度，不經照片 JPEG 轉檔流程。
- `[照片]` 分頁可使用手機相機拍照或選擇圖片，按下底部 `[送出]` 後，照片會廣播到 `/player` 與 `/combo` 播放畫面並限時顯示。
- `[取消]` 與需要確認送出的分頁之 `[送出]` 固定在面板最底部；貼圖分頁點擊即送，不顯示底部 `[送出]` 按鈕。分頁內容過長時，只捲動目前分頁內容，不會把底部操作列推出畫面。
- 照片只接受 JPEG、PNG、WebP，瀏覽器會先縮放至最長邊 1280px 並轉為 JPEG；伺服器驗證後限制 5 MB，不將照片保存到磁碟。
- 貼圖只允許 `static/stickers/` 內副檔名為 GIF、WebP、PNG 的檔案，單檔不得超過 2 MB；Socket.IO 只接受檔名，不接受用戶端提供的路徑或任意 URL。
- `/player` 與 `/combo` 收到合法事件後，從專案內的 `static/sounds/clap.wav` 載入真實拍手採樣，最多播放前 4 秒，並套用約 0.08 秒淡入與 0.35 秒淡出，不修改歌曲檔案，也不改變歌曲音量、KEY、播放進度或音訊模式。
- 音效事件只允許伺服器列出的效果名稱；未知效果會被忽略，避免任意資料觸發未定義的音訊行為。
- 播放端尚未由使用者啟動音效引擎時，瀏覽器可能因自動播放政策而暫不播放音效；先點擊播放端的「啟動音效引擎」即可恢復完整功能。
舊歌曲若只有兩聲道，播放器使用相容模式：原唱取左聲道、伴奏取右聲道，導唱播放兩聲道混合內容。

### 升降 KEY 設定規則

每首歌結束後，升降 KEY 必須自動回到「原 KEY」（音高偏移 `0`）；音訊模式（原聲/導唱/伴奏）、音量、字幕顯示與比例、畫面延遲及其他播放設定均保持不變。手動將升降 KEY 回到「原 KEY」時，也只重設音高偏移；音訊模式切換不得自行修改升降 KEY。

### 播放佇列與控制事件規則

`playlist_queue[0]` 永遠代表目前播放中的歌曲，後續項目依序代表待播歌曲。所有播放端、遙控端與整合端都必須以伺服器廣播的佇列和播放事件為準，不得自行猜測下一首歌曲。

`/remote` 與 `/combo` 的點歌面板會依伺服器廣播的佇列標示歌曲狀態：目前歌曲顯示「播放中」、後續歌曲顯示「已排隊」。已排隊歌曲仍可再次點播，讓使用者能依需求重複加入同一首歌曲；只有送出請求後的短暫等待期間會停用該次按鈕。

#### 切歌規則

前端的 `[切歌]` 只送出一個 Socket.IO 事件：

```text
control: cut
```

後端收到後必須依序執行：

1. 廣播 `stop_video`，停止目前歌曲。
2. 移除 `playlist_queue[0]`。
3. 將 `current_pitch` 重設為 `0`，其他播放設定保持不變。
4. 廣播 `apply_effect`，同步新的 KEY。
5. 廣播 `update_queue`，同步剩餘佇列。
6. 若仍有歌曲，廣播 `play_video`，其 payload 必須使用新的 `playlist_queue[0]`。
7. 廣播 `current_song`，同步目前歌曲與字幕狀態。

待播清單只剩一首時，切歌後只執行停止、移除、重設 KEY、更新空佇列，不得產生不存在的下一首；畫面應回到待機狀態。正常播放結束則由前端送出 `song_ended`，後端採用相同的佇列推進規則。兩者的差別是：切歌是使用者主動跳過歌曲，正常結束是影片 `ended` 事件觸發。

#### 播放事件的前端契約

- 收到 `stop_video` 時，前端停止目前媒體、清除字幕與播放來源，回到待機畫面。
- 收到 `play_video` 時，前端必須提高播放世代編號，重新設定媒體來源，等待 `loadedmetadata` 後再呼叫 `play()`。
- 舊歌曲的非同步 `loadedmetadata` 或 `play()` 回呼不得覆蓋較新的 `play_video`；必須檢查播放世代是否仍相同。
- `play_video` 的播放失敗只能顯示需要使用者啟動的提示，不得修改伺服器佇列。
- `stop_video` 與 `play_video` 必須由同一個 Socket.IO 連線按伺服器送出順序處理，避免舊停止事件清除下一首的媒體來源。

#### 重唱規則

重唱目前歌曲是將同一檔案重新插入目前歌曲之後，不是歌曲結束，因此不得呼叫會重設 KEY 的正常結束流程。重唱時目前的 `current_pitch` 必須保持不變，例如目前為 `-3`，重唱後仍為 `-3`。

### Socket.IO 廣播相容性規則

本專案使用的 Flask-SocketIO 版本中，事件處理器內的 `emit()` 與伺服器物件的 `socketio.emit()` 參數不同：

- 在 `@socketio.on(...)` 事件處理器中，使用 `emit('event', data, broadcast=True)` 將事件送給所有客戶端（雲端模式下由 `emit()` 自動限定在目前 `_CURRENT_CLOUD_ROOM` 房間內廣播）。
- 使用 `socketio.emit('event', data)` 時，不得傳入 `broadcast=True`。目前版本不接受這個參數；本機模式不指定 `to` 時會送給所有連線，雲端模式則由 `_emit_to_current_cloud_room` 自動附加目前房間 `to=room_code`。
- 兩種 API 不得混用參數規則，尤其不可把 `broadcast=True` 直接加到 `socketio.emit()`。

曾發生的切歌問題原因是 `handle_control('cut')` 使用了不相容的 `socketio.emit(..., broadcast=True)`。Flask-SocketIO 因此拋出：

```text
TypeError: emit() got an unexpected keyword argument 'broadcast'
```

例外發生在切歌流程中間，導致後面的 `update_queue` 與 `play_video` 沒有執行，使用者看到的結果就是歌曲停止或畫面停住，卻沒有自動播放下一首。修正方式是移除直接 `socketio.emit()` 的 `broadcast=True`，保留事件與 payload 不變。

### 一般歌詞（無時間碼）

歌詞不一定要提供原始時間碼。管理端的「既有歌曲歌詞轉檔」可以直接貼上一般歌詞，每行一句；轉檔後的 WebVTT 一律每句一個 cue，不把多句歌詞塞進同一 cue。歌曲長度超過 30 秒時，時間軸以前後各約 15 秒為緩衝，剩餘時間平均分配給歌詞行；第一句從 0 秒開始並延長，最後一句延續到歌曲結束。較短歌曲則在整段片長內平均分配。播放端「字幕：一般」依目前 cue 與後續 cue 建立約 12 行的右側深色半透明視窗；目前歌詞置於第 7 行附近，前置歌詞不足時以空白行補位，首段約 15 秒前導緩衝後才開始平順上移，後續依 cue 時間逐行移動。這種自動時間換算不保證逐句精準同步，但可同時保留前後歌詞，降低文字段落與歌曲實際演唱不完全吻合的影響。既有以 `PLAIN_LYRICS_` 標記、每個 cue 內含多行重疊窗口的舊 VTT 仍可播放與編輯。

線上歌詞搜尋中的 `lyrics.ovh` 純文字結果也可以直接選擇並儲存，會套用相同的一句一 cue 自動時間換算。已有 LRC、SRT 或 VTT 時，仍會優先保留原本的精準時間碼。

### 隨機播放與手動點歌互斥規則

為避免「有開啟隨機播放、但使用者手動點歌時被背景自動切歌」的競態條件，播放伺服器在以下規則下運作：

1. `random_play_enabled` 只有在待播清單為空時，才允許補一首隨機歌曲。
2. 任何使用者手動 `add_to_queue` 事件都會標記最後一次使用者操作時間。
3. `song_ended` 進入空隊列後，只有在該冷卻期間過後且仍為空隊列時，才允許觸發隨機補播。
4. 啟用/停用「無點歌時隨機播歌」本身不算手動點歌，因此不應更新 `last_user_action_time`；否則空隊列時會被 1.5 秒冷卻鎖住，無法自行偵測空隊列並開始播放。
5. 這樣可保證手動點歌不會在未明確按下切歌的情況下，被 random idle fill 直接頂掉當前播放。
6. 切歌後若待播清單已空，仍必須重新檢查「無點播時隨機播歌」：若該開關已勾選，就必須直接補上一首隨機歌，不能只在正常播放結束時才補播；否則使用者按下 [切歌] 後會出現「沒歌可播、畫面停住」的判斷漏洞。

這是正式的播放流程保護機制，與單曲新增、批量新增、既有歌曲處理流程共用同一套播放狀態管理原則。

## 管理端功能

- **新增 KTV 歌曲**：先依一般音訊規則製作；可量測音訊指標超出門檻時，寫入特殊歌曲 metadata，並在選歌介面顯示超標項目與實測值；結構、完整性或量測失敗仍拒收。
- **批量新增 KTV 歌曲**：每行格式為 `YouTube 網址 | 歌名`，逐首套用相同流程；特殊歌曲的所有超標項目寫入 MP4 metadata，失敗項目會記錄在批量錯誤檔案與可重試格式中。
- **批次修改歌名**：在管理端進階功能每行輸入 `舊歌名 | 新歌名`；逐行檢查來源、重複名稱、目的檔衝突及待播狀態。每列獨立改名 MP4、同名 `.vtt`、`.srt`、`.lrc` 與歌曲備註；單列失敗只回滾該列，記錄行號、舊名、新名及錯誤原因，並繼續處理其他列。全部完成後彙總成功與失敗清單，不因單列失敗回滾其他成功項目。
- **既有歌曲音訊平衡**：重新量測 c0/c1、c2、c4，輸出統一為 `5.1`。
- **六聲道逐聲道播放檢查**：管理端可選擇歌曲並單獨播放 c0～c5；每次只輸出選定聲道到左右喇叭，不修改歌曲檔案，供確認 c2 人聲、c4 伴奏規則。
- **既有歌曲音訊平衡（批次）**：管理端可選擇整個資料夾，先記錄全部 MP4，再逐首上傳與處理；每首成功驗證後才取代原檔，單首失敗不會中斷後續歌曲，避免一次上傳整個資料夾造成瀏覽器或伺服器無回應。
- **既有影片 AI 去人聲**：使用與新增歌曲相同的 Spleeter、伴奏來源、聲道與驗證規則。
- **既有影片效能最佳化**：轉為 H.264、最高 720p、30fps。
- **歌詞工具**：支援 SRT、LRC、VTT，統一儲存為 WebVTT。
- **手動歌詞預覽**：提供「一般／單行／雙行／雙行漸變」四種顯示模式，並可用模式按鈕左右的「−／＋」以 10% 階距調整預覽字級（60%–200%）；一般模式以目前 cue 為起點顯示 12 行視窗，動態模式依 WebVTT cue 與空白時間 slot 排列，雙行漸變依目前 cue 時間更新高亮；預覽模式不改變 VTT 儲存格式。

### 批次平衡防錯規則

- 批次開始時必須固定輸入檔案清單與總數；不可依處理中的暫存檔數量推測成功數。
- 每首歌曲都必須獨立建立暫存輸出，完成 FFprobe 六聲道、必要的模式 LUFS/True Peak 與 c4 LRA 量測驗證後，才可使用原子替換取代原檔；超出標準的可量測指標一律寫入特殊歌曲 metadata 並於選歌介面顯示，不設數值上限；聲道、檔案完整性或量測失敗仍拒絕。
- 單首 FFmpeg 工作設有逾時；逾時或驗證失敗必須記錄檔名與錯誤，繼續處理後續歌曲，並在批次結束後逐首重試失敗清單。
- 編碼後響度校正最多只允許一次；校正仍未通過時必須標記失敗，不得繼續遞迴重編碼或以降低驗證標準掩蓋問題。
- 批次 API 只有在失敗清單為空時才回傳 `success: true`；`success_count` 必須等於輸入總數，才可視為批次完成。
- 最終交付前必須重新掃描完整輸入清單，確認 `成功數 = 總數`、所有檔案為六聲道 `5.1` 且可完整解碼；c0/c1 盡量接近 `-14 LUFS`，偏差可接受且不要求 `-1 dBTP`；c4 一般標準為約 `-14 LUFS`、True Peak 不高於 `-1 dBTP`、LRA 不高於 `8 LU`。超標項目可作為特殊歌曲收錄，但 metadata 項目與成品實測值必須一致；聲道、完整性與必要量測仍不可豁免。

## 開發環境

### 1. 本機／區域網路版啟動

需求：Python 3.8 64 位元、FFmpeg、Spleeter 2.0.2、TensorFlow 2.3.0。

```powershell
cd "D:\Buff\Cursor資料夾\OpenKTV-ianAuto360"
.\.venv\Scripts\Activate.ps1
python main.py
```

使用本專案的 `main.py`、`啟動OpenKTV.bat` 或打包 EXE 啟動時，會先顯示啟動畫面與目前檢查狀態，再執行 Server 防重檢查。程式會依專案目錄取得 Windows 單實例鎖；若同專案已有鎖定中的舊 Server，會先送出 GUI 正常關閉訊息並等待退出，逾時後才依鎖檔記錄的 PID 回收。若舊版沒有單實例鎖，啟動器會檢查 `5000` 埠 owner，並辨識 `ianAutoKTV_Server.exe` 或本專案 `.venv\Scripts\python.exe` 執行的 `main.py`，可跨不同安裝目錄取代舊 KTV Server；其他未辨識程序佔用 `5000` 時會停止啟動，不會終止該程序。Spleeter 的 Windows worker 不取得單實例鎖，只執行分離工作，不得啟動 Flask 伺服器或佔用 `5000`；打包版啟動時會先呼叫 multiprocessing 的 `freeze_support()`，再略過 Server 啟動流程，確保 worker 能執行分離工作。

啟動後使用 HTTPS 區域網路網址：

- `/player`：播放端
- `/remote`：手機遙控端
- `/admin`：管理端
- `/combo`：播放與遙控整合端

首次使用每台裝置時，需接受自簽 HTTPS 憑證，才能使用完整的 Web Audio 與升降 KEY 功能。

### 2. 雲端房間版（Render 模式）本機測試啟動

可直接透過 `render_app.py` 在本機啟動雲端房間模式並測試預設 QNAP NAS 歌庫與房間流程：

```powershell
cd "D:\Buff\Cursor資料夾\OpenKTV-ianAuto360"
$env:QNAP_SHARE_URL = "https://www.myqnapcloud.com/smartshare/..."
$env:QNAP_SHARE_PASSWORD = "分享密碼"
.\.venv\Scripts\python.exe render_app.py
```

啟動後於瀏覽器開啟 `http://127.0.0.1:5000` 即可測試「建立房間／加入房間」、主點歌面板自動載入預設 NAS 歌單，以及點擊下載點歌功能。生產環境部署請參閱 [RENDER_DEPLOY.md](./RENDER_DEPLOY.md)。

## 打包與更新

完整發佈包：

```powershell
.\build_release.ps1
```

完整更新包：

```powershell
.\build_update.ps1
```

只修改 `templates` 或 `static` 內的檔案時可建立前端更新包；所有更新包都會完整包含 `static` 目錄及其子資料夾：

```powershell
.\build_update.ps1 -FrontendOnly
```

完整更新時必須一起替換 EXE、`_internal`、FFmpeg、yt-dlp 與 templates；使用者的 `ktv_songs` 與模型資料不需替換。修改 `main.py`、FFmpeg 流程、模型流程或打包設定時，不可只替換 EXE。

## 必要檔案

```text
ianAutoKTV/
├── main.py                      # 本機／區網版主程式與共用播放／音訊核心
├── render_app.py                # Render 雲端房間版 WSGI 入口與本機雲端測試入口
├── qnap_share_cloud.py          # 雲端版預設 QNAP NAS 房間歌庫連線與下載模組
├── google_drive_cloud.py        # 雲端版房間成員 Google Drive 個人歌庫模組
├── google_drive_prototype.py    # Google Drive 底層 API、快取索引與本機版共用模組
├── render.yaml                  # Render Blueprint 部署設定
├── requirements-render.txt      # Render 雲端版 Python 套件需求
├── RENDER_DEPLOY.md             # Render 雲端部署與環境變數說明
├── ianAutoKTV_Server.exe        # 打包版執行檔
├── yt-dlp.exe
├── ffmpeg/bin/ffmpeg.exe
├── templates/
├── static/
├── pretrained_models/2stems/
└── ktv_songs/
```

## 狀態列與除錯顯示

播放頁面上的 `engine-status` 區塊預設為關閉，避免在正常播放時塞滿除錯資訊。當伺服器端 `engine_status_visible` 打開時，系統會顯示一行壓縮版播放歷史，例如：

```text
播放歷史：A 一路向北 | B 想和你看海 | C 你是我的唯一 | ...
```

伺服器端的 `engine_debug_enabled` 預設為關閉；只有勾選「啟用音訊引擎除錯資訊」時，才會顯示完整音訊引擎診斷資訊，未勾選時顯示播放清單，例如：

```text
音訊引擎：SoundTouch AudioWorklet｜即時 LUFS：-14.2｜聲道模式：5.1｜WebAudio：6ch/explicit｜啟用：c0,c1
```

播放端連線後會主動重新請求 `engine_status_config`，除錯模式啟用時狀態列會提高顯示層級並使用高對比背景，避免開關已開啟但診斷資訊被播放器畫面遮住。

這兩個開關都由伺服器端控制，並透過 SocketIO 同步到所有播放端。正常使用時不建議開啟，僅在調校音訊/延遲/聲道時使用。

## 注意事項

- yt-dlp 應使用專案內官方獨立執行檔，避免使用綁定錯誤 Python 路徑的 launcher。
- Spleeter 需要 CPU 與記憶體，歌曲製作期間請等待處理完成。
- Spleeter 分離品質依歌曲混音而異；系統會固定套用伴奏來源與聲道規則，但模型仍可能留下少量人聲殘留。
- 版權影音僅供程式交流與個人家庭娛樂使用，請遵守相關法律與平台規範。
## 2026-10-18 初步 UI 里程碑

目前 `/remote` 已提供「Google Drive 歌庫」面板：區網遙控器可讀取 KTV 主機管理者選定的資料夾、查看 MP4 歌曲，按下「下載並點歌」後由伺服器使用主機端已連結帳號下載並加入共用待播清單。前端不接觸 OAuth 權杖；下載檔完成後沿用既有 `/api/list`、`/songs/<filename>` 與 Socket.IO 點歌流程。

本階段僅代表離線測試與初步 UI 流程完成；真實 Google 授權、瀏覽器播放及六聲道／字幕／KEY 相容性仍須依 [ToDo.md](./ToDo.md) 的未完成項目進行實機驗收。
### 2026-10-18 部署設定檔讀取修正

伺服器會從 KTV 安裝目錄讀取 `google_drive_deploy.json`；若該正式檔尚未建立，且同一目錄有使用者填妥的 `google_drive_deploy.example.json`，則會回退讀取 example 檔。打包腳本只會附上範本，不會自動複製被 `.gitignore` 排除的正式設定；部署時請將填妥的設定放在 EXE 旁。`google_drive_account.bin` 則在該安裝首次完成 Google 授權後才建立，並使用 Windows DPAPI 保護，不應從開發機複製。正式環境仍應使用 `google_drive_deploy.json`，並避免將 Client Secret 與 API key 提交至版本庫。
## 2026-10-18 真實 Google Drive 驗收進度

已完成一次真實驗收：KTV 主機可使用 Google OAuth 登入已連結帳號、保留並讀取 `ianAutoKTV` 資料夾；`/remote` 可顯示雲端 MP4，按下「下載並點歌」後由伺服器完整保存至 `drive_downloads/`，並加入共用待播清單。`/player` 已確認透過既有 `/songs/<filename>` 本機播放端點播放，HTTP Range 回應為 `206`，播放器實際載入完成且正在播放。

本次驗收尚未涵蓋其他瀏覽器／手機／電視、六聲道 Web Audio 分流、字幕、KEY、暫停／拖曳 seek／重播，以及權限撤銷和檔案刪除等完整錯誤情境；這些項目仍以 [ToDo.md](./ToDo.md) 未勾選內容為準。OAuth fallback link 只在 KTV 主機本機頁提供，遇到系統瀏覽器未開啟時可手動完成授權。
## 2026-10-18 伺服器端登入入口

伺服器主視窗的本機連結區已新增「Google Drive 歌庫（登入／選資料夾）」入口，會使用 `https://127.0.0.1:5000/drive-prototype` 開啟本機限定的 OAuth／Picker 頁面。Google 帳號連結與資料夾選擇不得使用區網 IP 或遙控器入口，避免被本機限定保護機制拒絕。
## 2026-10-18 OAuth 回呼完成後關閉回呼頁

OAuth loopback 回呼服務在完成授權碼交換、取得並保存 refresh token 後，只回傳成功訊息並嘗試關閉目前的 OAuth 回呼分頁，不再導向或重新開啟 `/drive-prototype`。原本的 KTV 本機驗證頁會持續輪詢授權狀態並開啟 Picker；若瀏覽器禁止回呼頁自動關閉，頁面會提示使用者手動關閉該分頁。OAuth 錯誤仍維持原本的錯誤頁面與流程狀態。

若原本的驗證頁已被瀏覽器取代，伺服器會重用尚未領取 Picker 短效權杖的 `authorized` flow，讓「選擇歌曲資料夾」可以正常繼續，不會將已完成的登入誤判為重複驗證流程。
## 2026-10-18 遙控器雲端歌庫按鈕配色

`/remote` 與 `/combo` 共用的雲端歌庫面板，將「重新整理雲端歌庫」設定為低飽和淺青綠色，將歌曲列上的「下載並點歌／點歌」設定為低飽和暖米色；兩者均使用深色文字、明確 hover／focus 狀態及柔和 disabled 狀態，維持原有功能與版面配置。

## 2026-10-18 遙控器雲端歌庫收合與歌曲來源架構決策

- [X] 將遙控器的「☁ Google Drive 歌庫」面板改為預設隱藏，由標題列左上角的雲端按鈕展開；設定按鈕維持在右上角，既有重新整理、下載並點歌及錯誤提示流程不變。
- [X] 確認後續採用「歌曲來源個別，播放佇列共用」：每位遙控器使用者未來可使用自己的 Google Drive 歌曲來源，歌曲完成下載後仍加入同一台 KTV 的共用播放佇列；本次先完成面板入口，不提前改動現有伺服器端共用帳號授權流程。

## 2026-10-18 遙控器入口按鈕配色微調

- [X] 保留右上角設定按鈕原本的灰藍色樣式；左上角 Google Drive 按鈕改為較高對比的柔和青綠色與白色圖示，補充 hover／focus 狀態，不變更按鈕位置與操作流程。

## 2026-10-30 雲端房間版預設 QNAP NAS 歌庫與介面整合

- [X] 新增 `qnap_share_cloud.py` 支援 QNAP myQNAPcloud SmartShare（CloudLink + File Station API）與 `share.cgi` 分享連結解析、遞迴掃描 `.mp4` 與配對同名 `.srt`／`.lrc`／`.vtt` 字幕，並支援透過環境變數 `QNAP_SHARE_URL` 與 `QNAP_SHARE_PASSWORD` 預熱快取預設 NAS 歌單。
- [X] 雲端模式下進入 `/remote` 或 `/combo` 時，預設 NAS 歌單自動整合至主畫面「🎶 點歌」面板（支援搜尋、字數／主唱者／語言／歸屬篩選、排序與字幕標籤），點擊「播歌」自動下載至房間快取（保留原始 MP4 檔名）並加入待播清單；左上角「☁ 雲端歌庫」視窗專屬於個人 Google Drive 歌庫。
