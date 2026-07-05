# Native Tool Context Injection System — 技術計劃

## 背景問題

### 核心問題
1. **Hermes Gateway 是 Server-Side Tool Execution**
   - Gateway 在 server 端執行 tools，模型看不到完整 tool result
   - 歷史訊息中只有簡短佔位符（如 `已分析 terminal > echo meow meow m...`）
   - 這導致**模型失憶** — 在後續回合中不知道之前的 tool results

2. **Chat Completions API 無法原生渲染 tool calls**
   - OpenWebUI v0.10+ 在 **Responses API** 模式下使用 structured output
   - 但 Hermes Gateway 使用 **Chat Completions API**
   - 在 CC 模式下，`hermes.tool.progress` 事件**不會被前端渲染**

3. **重骰無效**
   - 每次重骰都產生新訊息，上下文不一致
   - 歷史訊息中沒有完整 tool results

---

## 解決方案架構

### 組件 1：完全透傳 Filter (Passthrough Mode)

**目標**：在 enhance-v2 模式下，**完全不修改** Hermes Gateway 的 SSE stream

**行為**：
- 透傳所有 `data:` chunks（包括 `hermes.tool.progress` 事件）
- **不注入任何 `<details>` HTML 到 `delta.content`**
- **不發送任何 keepalive/nudge**（除非必要）
- 只處理 `[DONE]` 和 EOF

**優點**：
- 不破壞任何原生格式
- 與 Responses API 模式兼容
- 最小化中間件干擾

---

### 組件 2：Tool Event 捕獲與簡短通知

**目標**：捕獲 `hermes.tool.progress` 事件，向用戶顯示簡短狀態

**行為**：
- 捕獲 `status="running"` → 顯示 "🔧 執行 terminal"
- 捕獲 `status="completed"` → 顯示 "✅ 已完成 terminal"
- 這些是**簡短的可見內容**，注入到 `delta.content` 中
- **不注入完整 tool result** — 那將由組件 3 處理

**技術細節**：
- 在 `transform_stream` 中捕獲 `hermes.tool.progress` 事件
- 使用 `_build_content_chunk()` 注入簡短狀態
- 格式：`🔧 執行 {tool_name}` 或 `✅ 已完成 {tool_name}`

**優點**：
- 用戶可以看到工具執行狀態
- 不破壞原生格式
- 輕量、無副作用

---

### 組件 3：Persistent Tool Context Cache (核心創新)

**目標**：在會話級別暫存完整 tool results，並注入到歷史訊息中

**架構**：
- **Storage**：SQLite 或 JSON file（輕量、持久化）
- **Key**：`session_id + message_id`（使用 session marker 系統）
- **Data**：完整 tool result（arguments + result）
- **TTL**：會話級別（可配置，如 24 小時）

**工作流程**：
1. **捕獲**：在透傳時，捕獲 `hermes.tool.progress` 事件中的完整 tool result
2. **存儲**：將 tool result 存儲到 cache，key 為 `session_id + message_id`
3. **注入**：當 Hermes Gateway 發送包含歷史訊息的 request 時，攔截並注入完整 tool results

**Storage Schema (SQLite)**：
```sql
CREATE TABLE tool_context (
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments TEXT,
    result TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id, message_id, tool_call_id)
);
```

**優點**：
- 持久化存儲
- 會話級別隔離
- 輕量、高效

---

### 組件 4：Session Marker 檢測與歷史注入

**目標**：檢測會話隔離，並向歷史訊息注入完整 tool results

**現有 Session Marker 實作**：
- **格式**：` ```session\n{session_id}  {timestamp}\n``` `
- **位置**：插入到第一個 user message 的末尾
- **檢測**：在 request body 中搜尋此格式
- **用途**：會話隔離、cache key、重骰檢測

**行為**：
- **檢測**：在收到的第一個 message 中，檢測 session marker
- **攔截**：如果檢測到 marker，攔截整個 request body
- **注入**：從 cache 中讀取該 session 的所有 tool results，注入到歷史訊息中的佔位符位置
- **转发**：將修改後的 request 转发到 Hermes Gateway

**技術細節**：
- 在 `handle_completions_request` 中攔截 request body
- 使用 `get_or_create_session_id` 檢測 session marker
- 從 cache 中讀取 tool results
- 注入到歷史訊息中的佔位符位置（如 `[TOOL_RESULT:tc_id]`）

**優點**：
- 使用現有 session marker 系統
- 會話級別隔離
- 與重骰兼容

---

## 實施步驟

### Phase 1: Passthrough Filter
- [ ] 創建新分支 `feature/native-tool-context`
- [ ] 實作完全透傳模式（不修改任何 SSE data）
- [ ] 測試：使用 curl 驗證透傳

### Phase 2: Tool Event 捕獲與簡短通知
- [ ] 實作 `hermes.tool.progress` 事件捕獲
- [ ] 實作簡短狀態通知（注入到 `delta.content`）
- [ ] 測試：在 OpenWebUI 中驗證顯示

### Phase 3: Persistent Cache
- [ ] 實作 SQLite storage schema
- [ ] 實作 tool result 存儲邏輯
- [ ] 實作 TTL 和清理邏輯
- [ ] 測試：使用 Python script 驗證存儲和讀取

### Phase 4: Session Marker 檢測與歷史注入
- [ ] 實作 session marker 檢測
- [ ] 實作 request body 攔截和修改
- [ ] 實作 history injection 邏輯
- [ ] 測試：使用 curl 驗證注入

### Phase 5: 整合測試
- [ ] 在 OpenWebUI web 前端測試
- [ ] 在 Conduit app 測試
- [ ] 測試重骰功能
- [ ] 測試會話隔離

---

## 風險與緩解

1. **風險**：Request body 攔截可能破壞 Hermes Gateway 的預期格式
   - **緩解**：只在檢測到 session marker 時攔截，否則透傳

2. **風險**：Cache 可能變得過大
   - **緩解**：實作 TTL 和定期清理

3. **風險**：Session marker 可能與用戶輸入衝突
   - **緩解**：使用唯一格式（如 ` ```session\n{uuid4}\n``` `）

4. **風險**：History injection 可能導致 token 限制
   - **緩解**：實作 smart truncation（只注入最近的 N 個 tool results）

---

## 現有 Session Marker 實作參考

### 目前實作位置
- **檔案**：`/home/thomas2018/hermes_tool_filter/main.py`
- **函數**：`get_or_create_session_id()`, `update_session_id()`
- **格式**：` ```session\n{session_id}  {timestamp}\n``` `
- **檢測**：使用 `marker_pattern` 和 `legacy_pattern` regex

### 目前行為
1. **新會話**：在第一個 user message 中注入 timestamp marker
2. **後續請求**：從 assistant history 中檢索 marker
3. **Cache**：使用 `_session_cache` 和 `_pending_session_markers` 字典

### 可用函數
- `get_or_create_session_id(messages)` → 返回 session_id
- `update_session_id(messages, new_sid)` → 更新 cache
- `derive_session_id(messages)` → 從 messages 派生 session_id
- `_strip_timestamp_and_derive(messages)` → 移除 timestamp 後派生

---

## 結論

這是一個全新的架構，需要：
1. **完全透傳** — 不修改 SSE stream
2. **捕獲 tool events** — 顯示簡短狀態
3. **Persistent cache** — 存儲完整 tool results
4. **History injection** — 向歷史訊息注入完整 results

**關鍵創新**：使用現有 session marker 系統進行會話隔離和 cache key。