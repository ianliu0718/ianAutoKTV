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

## 每個房間連結自己的 Google Drive

1. 房主在播放器頁面點「設定房間歌庫／開啟遙控器」，再於遙控器的「Google Drive 歌庫」面板選「連結 Google Drive 帳號」。
2. 完成 Google 授權後選「從 Google Drive 選取歌曲」，在 Picker 多選 MP4；若要歌詞同步，也一併選取同名 `.vtt` 檔。
3. 選取的檔案加入目前房間的歌庫；房間內的遙控器都能點歌。點歌時 Render 會將已轉好的 MP4（及選取的 VTT 歌詞）暫存到該房間專屬目錄，再加入待播清單，不會修改 Google Drive 原檔。

Refresh token 只保存在 Render 程序記憶體的房間狀態，不會寫入瀏覽器 Cookie 或程式碼。Render 重啟會清除房間與授權，房主需重新建立房間並連結 Drive。此版本使用 `drive.file`，Picker 只授權房主明確選取的檔案；未選取的 Drive 檔案不會列出或讀取。若 OAuth 同意畫面仍在 Testing，只能授權列入測試使用者的帳號，refresh token 也可能在 7 天後到期。Render 會承接歌曲下載與播放流量；免費方案的暫存容量與流量需留意。

## 目前雲端模式

- Render 透過 HTTPS/WSS 提供既有 Flask 頁面與 Socket.IO；Render 提供 `PORT`。
- 多個房間可以同時運行。每個房間的佇列、Key、原聲／導唱／伴奏選擇、字幕與播放控制會各自隔離。
- 房間由單一 Render 程序記憶體保存；服務重啟、重新部署或免費服務休眠回收後，房間及其狀態會清除。閒置且無連線的房間會在 6 小時後清理。
- 房號是加入權限；請只把房號或 QR Code 分享給要加入的人。輸入房號有基本錯誤次數限制。
- 雲端模式關閉桌面轉檔、上傳、改名及下載等管理端點。Google Drive 連線以房間為單位；下載的歌曲只暫存於該房間目錄，歌曲媒體不會寫回 Google Drive。
- 舊的 Windows DPAPI／loopback Google Drive 原型不會在雲端模式註冊；Render 使用新的每房間 Web OAuth 授權狀態。

## Render 免費方案注意事項

免費 Web Service 閒置時可能休眠，喚醒需要等待；服務使用暫存磁碟，且單一程序記憶體狀態不具重啟持久性。NAS／WebDAV 歌庫與房間重啟後恢復仍需另行規劃。
