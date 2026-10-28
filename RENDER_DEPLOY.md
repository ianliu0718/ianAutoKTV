# Render 部署

Render 版本採用房間制：房主建立房間後，播放器顯示 8 位數房號與專屬 QR Code；其他人輸入房號或掃描 QR Code，即可加入遙控器。每個房間有獨立的待播清單、播放與字幕設定、升降 Key、音軌及播放控制狀態。

## 部署步驟

1. 將專案推送到 GitHub Repository。請勿提交 `google_drive_deploy.json`、`google_drive_account.bin`、`.env` 或歌曲媒體檔。
2. 在 Render 建立 Blueprint，選擇此 Repository 並使用 `render.yaml`。
3. Render 會自動產生 `SECRET_KEY`，並要求設定 `GOOGLE_CLIENT_ID`、`GOOGLE_CLIENT_SECRET`、`GOOGLE_PICKER_API_KEY`、`GOOGLE_PROJECT_NUMBER`。請在同一個 Google Cloud 專案啟用 Google Drive API 與 Google Picker API，建立 OAuth「Web application」用戶端與 API Key。

   OAuth 用戶端設定以下 Authorized JavaScript origin：

   `https://ianautoktv.onrender.com`

   並設定以下 Authorized redirect URI：

   `https://ianautoktv.onrender.com/api/drive/google/callback`

   若 Render 網址不同，請填入實際網址。API Key 限制可設定為 Websites，允許你的 Render 網域與 `https://docs.google.com/*`，並限制 Google Picker API。

4. 部署完成後打開 Render 服務網址，首頁會顯示「建立房間」及「加入房間」。
5. 在要播放歌曲的裝置選「建立房間」；要遙控的手機可掃描播放器 QR Code，或在首頁輸入房號。

## 房間成員各自連結自己的 Google Drive

1. 房主或任何已加入房間的遙控使用者，都可以在自己的遙控器「Google Drive 歌庫」面板點「連結我的 Google Drive 並選擇歌庫資料夾」。
2. 首次使用時完成 Google 授權，系統返回原頁後會自動開啟 Google Picker；若帳號已連結，按鈕會直接開啟 Picker。每個瀏覽器登入者看到並管理自己的 Google Drive 帳號及資料夾。
3. 系統會列出該使用者所選資料夾第一層內的 MP4，並以不分大小寫的同名方式配對 `.vtt`、`.srt` 或 `.lrc` 歌詞；有配對歌詞的歌曲會顯示 📝。子資料夾不會遞迴掃描。點歌時 Render 會將 MP4 及配對歌詞暫存到該房間專屬目錄，並加入房間共用待播清單，不會修改 Google Drive 原檔。

此流程使用 `https://www.googleapis.com/auth/drive.readonly`，讓系統能列出所選資料夾中的 MP4 與歌詞。請在 Google Cloud OAuth 同意畫面的「資料存取權」新增此唯讀範圍；Picker API Key、OAuth 用戶端、Render 環境變數名稱與 callback 網址維持不變。若之前已用舊範圍連結，請重新連結 Google 帳號並同意新的權限。

`drive.readonly` 是 Google 的受限制範圍。OAuth 同意畫面處於 Testing 時，只能授權列入測試使用者的帳號，refresh token 通常會在 7 天後到期；若要提供給一般使用者長期使用，可能需要完成 Google OAuth 驗證及相關安全審查。Refresh token 分別保存在 Render 程序記憶體中各房間的使用者狀態，不會寫入瀏覽器 Cookie 或程式碼。Render 重啟會清除房間、授權及暫存歌曲；各使用者需重新加入房間並連結 Drive。Render 會承接歌曲下載與播放流量；免費方案的暫存容量與流量需留意。

## 目前雲端模式

- Render 透過 HTTPS/WSS 提供既有 Flask 頁面與 Socket.IO；Render 提供 `PORT`。
- 多個房間可以同時運行。每個房間的佇列、Key、原聲／導唱／伴奏選擇、字幕與播放控制會各自隔離。
- 房間由單一 Render 程序記憶體保存；服務重啟、重新部署或免費服務休眠回收後，房間及其狀態會清除。閒置且無連線的房間會在 6 小時後清理。
- 房號是加入權限；請只把房號或 QR Code 分享給要加入的人。輸入房號有基本錯誤次數限制。
- 雲端模式關閉桌面轉檔、上傳、改名及下載等管理端點。Google Drive OAuth 與歌庫以房間內的瀏覽器使用者為單位隔離；下載的歌曲暫存於房間共用目錄，供該房間點歌播放，歌曲媒體不會寫回 Google Drive。
- 舊的 Windows DPAPI／loopback Google Drive 原型不會在雲端模式註冊；Render 使用按房間與瀏覽器使用者隔離的 Web OAuth 授權狀態。

## Render 免費方案注意事項

免費 Web Service 閒置時可能休眠，喚醒需要等待；服務使用暫存磁碟，且單一程序記憶體狀態不具重啟持久性。NAS／WebDAV 歌庫與房間重啟後恢復仍需另行規劃。
