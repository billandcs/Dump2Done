# Dump2Done Dashboard Next Steps

本備忘錄用來追蹤主控台接下來的 UI/UX 與功能施工項目。原則是先降低使用者認知負擔：先讓系統判斷，再顯示必要控制。

## 已決定的 UX 原則

- 初始狀態不顯示工程設定，例如 Profile、模型、解析度。
- 使用者選檔後，依檔案類型顯示適用設定。
- 圖片模式只保留圖片編輯必要項目：預覽、提示詞、輸出資料夾、快捷操作。
- 影片模式才顯示 pipeline 相關設定。
- 平台設定不要跨平台混列。Qualcomm 裝置預設只顯示 Qualcomm 相關選項與安全 fallback。
- 所有危險操作都採用二段確認或可恢復策略。

## 下一批施工項目

- [ ] 將檔案判斷從前端 MIME/副檔名升級為後端檢測結果回傳，避免瀏覽器 MIME 不準。
- [ ] 將影片上傳後的 queued job 接到真實 `pipeline/runner.py` 執行流程，而不是只保存 input。
- [ ] 影片 job 執行完成後自動刷新 gallery，顯示 `renders/*.mp4`。
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

1. 將影片 queued job 接到真實 runner，並讓 render 產物回到 gallery。
2. 後端回傳媒體檢測結果與適用控制。
3. Profile 選項由 active platform 生成。
4. 圖片 before/after 對照。
5. trash 復原與清空。
6. 拆分前端模板，降低後續維護成本。
