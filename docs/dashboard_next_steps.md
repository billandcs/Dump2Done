# Dump2Done Dashboard Next Steps

本備忘錄用來追蹤主控台接下來的 UI/UX 與功能施工項目。原則是先降低使用者認知負擔：先讓系統判斷，再顯示必要控制。

產品主軸是 local-first migration：能本地跑的先本地跑，雲端只作為可拔插 fallback。每一條雲端路線都要保留未來切回本地 provider 的介面與 artifact 格式。

## 已決定的 UX 原則

- 初始狀態不顯示工程設定，例如 Profile、模型、解析度。
- 使用者選檔後，依檔案類型顯示適用設定。
- 圖片模式只保留圖片編輯必要項目：預覽、提示詞、輸出資料夾、快捷操作。
- 影片模式才顯示 pipeline 相關設定。
- 平台設定不要跨平台混列。Qualcomm 裝置預設只顯示 Qualcomm 相關選項與安全 fallback。
- 所有危險操作都採用二段確認或可恢復策略。
- Provider 選擇要以「本地優先」呈現：先顯示本機可用/缺少什麼，再顯示可選雲端 fallback。
- 雲端功能不可暗中自動執行；需要明確設定與可見提示。
- 任務 artifact 要避免綁死單一 provider，讓同一個 job 將來能從 OpenAI fallback 改成本地 ComfyUI / ONNX / QNN。

## 下一批施工項目

- [x] 設定頁新增 provider health cards：Pillow、Automatic1111、ComfyUI、OpenAI key、Ollama、ASR、FFmpeg。
- [ ] 將 image edit provider 抽成明確 registry，回傳每個 provider 的 ready/missing/blocker。
- [ ] ComfyUI 支援載入 workflow JSON，真正送出 prompt queue 並收回產物。
- [ ] OpenAI fallback 加上明確確認流程，不在 Auto 模式下暗中送雲端。
- [x] 新增「本地化進度」區塊：目前哪些能力已本地、哪些仍需外部服務、下一步如何切回本地。
- [ ] 將檔案判斷從前端 MIME/副檔名升級為後端檢測結果回傳，避免瀏覽器 MIME 不準。
- [x] 將影片上傳後的 queued job 接到真實本地 video edit runner，而不是只保存 input。
- [x] 影片 job 執行完成後自動刷新 gallery，顯示 `renders/*.mp4`。
- [ ] 將影片衣服換色從中央區域 deterministic MVP 升級為人物/衣服 segmentation + tracking。
- [ ] 影片上傳後讀取基本 metadata，再決定解析度選項，例如橫式/直式、長短片、4K 降載選項。
- [ ] Profile 選項改由後端根據 `check_env.py` 的 active platform 產生。
- [ ] 設定頁新增「平台策略」區塊，只顯示目前硬體平台的推薦路線。
- [ ] 圖片編輯結果新增 before/after 對照檢視。
- [ ] 刪除 trash 增加復原入口與清空 trash 功能。
- [ ] 任務佇列加入狀態篩選：全部、執行中、完成、失敗。
- [ ] Console log 加入搜尋與只看目前 job 的切換。
- [ ] 將多國語系字串從 inline JS 拆成獨立資源，避免 `server.py` 持續膨脹。
- [ ] 將 Dashboard HTML 拆出 template 檔或前端資源檔，保留 Python server 負責 API。

## 近期優先順序建議

1. ComfyUI workflow JSON 路由，讓生成式圖片開始有真正本地替代 OpenAI 的路線。
2. OpenAI fallback 明確確認流程，避免 Auto 模式悄悄送雲端。
3. 強化影片衣服換色：加入人物/衣服 segmentation + tracking，減少只靠中央區域遮罩的誤差。
4. 後端回傳媒體檢測結果與適用控制。
5. Profile 選項由 active platform 生成。
6. 圖片 before/after 對照。
7. trash 復原與清空。
8. 拆分前端模板，降低後續維護成本。
