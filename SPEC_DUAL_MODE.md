# Hermes API 雙模式 Proxy 升級技術規格書

| 項目 | 內容 |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-06-08 |
| 狀態 | 草案 / 待實作 |
| 作者 | Hermes Tool Filter Proxy 團隊 |

---

## 1. 專案背景與目標

### 1.1 現行狀況

目前的 Proxy 僅支援 OpenAI Chat Completions 格式（`POST /v1/chat/completions`），作為 Open WebUI 與多個 Hermes Gateway profiles 之間的透明代理路由器。功能包括：

- 多租戶路由（port 30000~30003 對應不同 profile）
- SSE 工具卡片增強（enhance-v2 模式）
- 對話歷史清理（History Sanitization，防止 context 污染）
- 自動分段（Auto Split）

### 1.2 核心痛點：Context 污染

當 Hermes Gateway 執行工具（tool call）時，會在 assistant 的回應中嵌入 `<details type="tool_calls">` HTML 標籤。這些標籤在下一輪對話中被原樣送回 LLM，導致：

- **Prompt 膨脹**：每次工具執行都累積原始 XML/HTML 結構，佔用 context window
- **LLM 理解干擾**：模型可能將 `<details>` 標籤當作內容而非結構標記
- **工具輸出洩漏**：敏感的 tool result 以原始格式暴露給模型

History Sanitization 在 Chat Completions 模式下可以緩解此問題，但 Responses API 模式有完全不同的歷史管理機制（`previous_response_id` 鏈），需要不同的處理策略。

### 1.3 升級目標

| 編號 | 目標 | 說明 |
|---|---|---|
| G1 | 雙模式支援 | 同時支援 Chat Completions 和 Responses API 兩種格式 |
| G2 | 模式切換 | 根據請求路徑自動偵測並切換處理模式 |
| G3 | SSE 格式轉換 | 兩種模式的 SSE 事件格式差異需正確透傳 |
| G4 | 工具過濾一致性 | 兩種模式下 `_` 開頭的內部工具（如 `_thinking`）均需排除 |
| G5 | 歷史管理對齊 | Session ID（Chat）與 previous_response_id（Responses）的對應處理 |
| G6 | 非破壞性 | 現行 Chat Completions 功能完全不受影響 |

---

## 2. 端點對比

### 2.1 Chat Completions 端點

    路徑: POST /v1/chat/completions
    認證: Authorization: Bearer <API_SERVER_KEY>
    請求體: JSON — 核心欄位為 messages (array)
    回應體: JSON 或 SSE stream
    狀態: 無狀態（stateless），opt-in 會話延續透過 X-Hermes-Session-Id 標頭
    會話延續: X-Hermes-Session-Id 標頭 + state.db 內部儲存
    長期記憶: X-Hermes-Session-Key 標頭（獨立於 Session-Id）

### 2.2 Responses API 端點

    路徑: POST /v1/responses
    認證: Authorization: Bearer <API_SERVER_KEY>
    請求體: JSON — 核心欄位為 input (string|array)
    回應體: JSON 或 SSE stream
    狀態: 有狀態（stateful），透過 previous_response_id 或 conversation 名稱鏈接
    會話延續: previous_response_id 欄位 或 conversation 名稱
    長期記憶: X-Hermes-Session-Key 標頭（同 Chat Completions）

### 2.3 Responses 額外端點

    路徑: GET /v1/responses/{response_id}
    功能: 取得已儲存的 response 物件
    回應: 完整的 response JSON（含 output items、usage）

    路徑: DELETE /v1/responses/{response_id}
    功能: 刪除已儲存的 response
    回應: {"id": response_id, "object": "response", "deleted": true}

### 2.4 認證方式（兩模式共用）

    標頭: Authorization: Bearer <key>
    驗證: HMAC-SHA256 比對（_check_auth 方法）
    失敗: HTTP 401 + {"error": {"message": "Invalid API key", ...}}
    注意: API_SERVER_KEY 為必填，即使綁定 127.0.0.1 也強制要求

### 2.5 共用標頭

    X-Hermes-Session-Id: 會話識別（Responses 模式在回應中回傳）
    X-Hermes-Session-Key: 長期記憶範圍標識（兩模式皆支援）
    Idempotency-Key: 等幂鍵（Responses 模式支援，防止重複提交）
    Content-Type: application/json

---

## 3. 請求格式規格

### 3.1 Chat Completions 請求

```json
{
  "model": "hermes-agent",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello, help me with Python code."}
  ],
  "stream": true
}
```

    必填欄位: messages (array)
    選填欄位: model, stream, temperature, max_tokens
    特殊處理: system message 轉為 ephemeral_system_prompt，不進入 conversation_history
    多模態: content 可為字串或 [{type, text/image_url}] 陣列
    工具定義: 來自 config.yaml platform_toolsets.api_server，NOT 來自請求體

### 3.2 Responses API 請求 — 基本型（字串輸入）

```json
{
  "model": "hermes-agent",
  "input": "Hello, help me with Python code.",
  "instructions": "You are a helpful assistant.",
  "stream": true
}
```

### 3.3 Responses API 請求 — 進階型（陣列輸入 + 歷史鏈接）

```json
{
  "model": "hermes-agent",
  "input": [
    {"role": "user", "content": "Previous context message"},
    {"role": "user", "content": "Current question"}
  ],
  "previous_response_id": "resp_a1b2c3d4e5f6...",
  "instructions": "Updated instructions override previous.",
  "store": true,
  "truncation": "auto"
}
```

### 3.4 Responses API 請求 — 會話名稱型

```json
{
  "model": "hermes-agent",
  "input": "Continue our discussion about databases.",
  "conversation": "my-database-project",
  "stream": false
}
```

### 3.5 Responses API 請求 — 自訂歷史型

```json
{
  "model": "hermes-agent",
  "input": "What is the capital of France?",
  "conversation_history": [
    {"role": "user", "content": "Let's talk about Europe."},
    {"role": "assistant", "content": "Europe has many countries."}
  ],
  "store": true
}
```

### 3.6 欄位對比

    Chat Completions:
      messages: array[object] — 完整對話歷史（含 system/user/assistant）
      stream: bool — 是否啟用串流
      model: string — 模型名稱

    Responses API:
      input: string | array[object] — 當次輸入（字串或訊息陣列）
      instructions: string — 系統指令（取代 system message）
      previous_response_id: string — 上一筆 response 的 ID
      conversation: string — 會話名稱（替代 previous_response_id）
      store: bool — 是否儲存（預設 true）
      truncation: string — "auto" 時自動截斷至最後 100 則
      conversation_history: array[object] — 自訂歷史（優先於 previous_response_id）
      tools: array — 工具定義（但 Hermes 實際使用 config.yaml 中的設定）

### 3.7 關鍵差異總結

    1. Chat 用 messages[] 傳完整歷史；Responses 用 input[] 傳當次輸入
    2. Chat 的 system message 在 messages 陣列中；Responses 用 instructions 欄位
    3. Chat 用 X-Hermes-Session-Id 標頭延續會話；Responses 用 previous_response_id
    4. Responses 支援 conversation 名稱自動鏈接（內部維護名稱→ID 映射）
    5. Responses 支援 conversation_history 欄位直接提供歷史（無須 server 端儲存）
    6. Responses 支援 Idempotency-Key 標頭；Chat 不支援
    7. Responses 的 store 預設 true；Chat 無此概念

---

## 4. Streaming 資料流規格

### 4.1 Chat Completions SSE 事件格式

    Content-Type: text/event-stream

    事件格式（標準 OpenAI 格式）:
      event: (無或空)
      data: {"id":"...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"..."}}]}

    事件格式（工具調用）:
      event: hermes.tool.progress
      data: {"type":"hermes.tool.progress","name":"tool_name","status":"started|progress|done",...}

    結束事件:
      data: [DONE]

    特點:
      - 每筆 delta 包含 role（首次）和 content（後續）
      - 工具進度透過 hermes.tool.progress 自訂事件
      - 結束以 [DONE] 標記
      - Proxy 攔截 hermes.tool.progress 事件，在 done 時注入 <details done="true">

### 4.2 Responses API SSE 事件格式

    Content-Type: text/event-stream

    事件類型（OpenAI Responses 規格）:

      response.created
        初始封包，status=in_progress，包含 response_id、model、created_at

      response.output_item.added
        新增 output item（message 或 function_call）
        data: {item: {type:"message"|"function_call", ...}, output_index: int}

      response.output_text.delta
        文字增量
        data: {item_id: "msg_...", delta: "text", output_index: int}

      response.output_text.done
        文字輸出完成

      response.output_item.done
        item 完成（含最終 arguments 或 content）
        data: {item: {...}, output_index: int}

      response.function_call_arguments.delta
        函數參數增量（串流中）

      response.function_call_arguments.done
        函數參數完成

      response.completed
        終止事件，包含完整 response 物件（output items + usage）

      response.failed
        錯誤終止事件

    特點:
      - 每筆事件含 sequence_number（單調遞增）
      - output_index 追蹤 item 順序
      - function_call 有獨立的 started/complete 回調
      - 沒有 [DONE]，以 response.completed 或 response.failed 結束

### 4.3 SSE 事件對比

    ┌─────────────────────────────┬────────────────────────────────┬──────────────────────────────┐
    │ 事件類型                    │ Chat Completions               │ Responses API                │
    ├─────────────────────────────┼────────────────────────────────┼──────────────────────────────┤
    │ 起始                        │ (無特殊事件)                   │ response.created             │
    │ 文字增量                    │ delta.content                  │ response.output_text.delta   │
    │ 文字完成                    │ (無特殊事件)                   │ response.output_text.done    │
    │ 工具開始                    │ hermes.tool.progress (started) │ response.output_item.added   │
    │ 工具參數                    │ hermes.tool.progress (args)    │ function_call_arguments.delta│
    │ 工具完成                    │ hermes.tool.progress (done)    │ response.output_item.done    │
    │ 工具結果                    │ (無獨立事件)                   │ function_call_output item    │
    │ 終止                        │ [DONE]                        │ response.completed           │
    │ 錯誤                        │ (無標準事件)                   │ response.failed              │
    └─────────────────────────────┴────────────────────────────────┴──────────────────────────────┘

### 4.4 Proxy 對 SSE 的處理策略

    Chat Completions 模式:
      - 攔截 hermes.tool.progress 事件
      - enhance-v2 模式：即時串流 + <details> 子標籤格式
      - enhance 模式：過濾 done=false + 注入帶 label 的完成標籤
      - passthrough 模式：直接透傳
      - strip 模式：移除 <details> 並替換為純文字
      - 根據 User-Agent 決定是否 strip（Conduit APP 需要 strip）

    Responses 模式:
      - 目前建議：直接透傳（passthrough），不進行工具卡片轉換
      - 原因：Responses 事件結構完全不同，無 hermes.tool.progress 事件
      - 未來可選：將 response.output_item.added/done 映射為 <details> 卡片
      - 注意：Responses 模式使用 tool_start_callback/tool_complete_callback
        而非 tool_progress_callback，事件流更結構化

---

## 5. Tool Calling 處理流程

### 5.1 工具來源

    兩模式共用同一工具來源:
      config.yaml → platform_toolsets.api_server

    工具定義不在請求體中，而是由 Hermes Gateway 從配置讀取。
    這意味著 Proxy 不需在請求中注入或修改 tools 欄位。

### 5.2 工具過濾規則

    規則: 名稱以 "_" 開頭的內部工具自動排除
    範例: _thinking, _search 等內部工具不會出現在工具卡片中
    實施位置: transform_stream() 內的 ToolCallBuffer

### 5.3 Chat Completions 模式工具流程

    1. LLM 產生 tool call → hermes.tool.progress (status=started)
    2. Proxy 收到 started 事件 → ToolCallBuffer 建立緩衝
    3. 工具執行中 → hermes.tool.progress (status=progress)
    4. 工具完成 → hermes.tool.progress (status=done)
    5. Proxy 在 done 時注入 <details done="true"> 標籤
    6. enhance-v2: 即時串流工具名稱 + 子標籤顯示參數

    流程圖:
      LLM → tool_progress_callback → Proxy 攔截 → 轉換/過濾 → 下游

### 5.4 Responses API 模式工具流程

    1. LLM 產生 tool call → tool_start_callback (tool_call_id, name, args)
    2. 工具執行中（無中間進度事件）
    3. 工具完成 → tool_complete_callback (tool_call_id, name, args, result)
    4. SSE 發出:
       - __tool_started__ → 映射為 response.output_item.added (function_call)
       - __tool_completed__ → 映射為 function_call_output item
    5. 最終 response.completed 包含所有 output items

    流程圖:
      LLM → tool_start_callback → Proxy 透傳 → ...
      LLM → tool_complete_callback → Proxy 透傳 → ...

### 5.5 關鍵差異

    1. Chat 用 tool_progress_callback（單一回調，多種 status）
       Responses 用 tool_start_callback + tool_complete_callback（分離）
    2. Chat 的 hermes.tool.progress 是 SSE 事件；
       Responses 的工具事件是內部 queue 項目，由 _write_sse_responses 轉換
    3. Chat 的工具事件包含 preview（摘要預覽）；
       Responses 的 function_call_output 包含完整 result
    4. Proxy 在 Chat 模式下對工具事件做轉換；
       Responses 模式建議初期直接透傳

---

## 6. 對話歷史管理機制

### 6.1 Chat Completions — Session ID 模式

    機制: 客戶端在標頭中傳入 X-Hermes-Session-Id
    歷史來源: state.db（Hermes 內部 SQLite）
    歷史載入: db.get_messages_as_conversation(session_id)
    歷史傳遞: 請求體中的 messages[] 包含完整歷史（或從 session 載入）
    Sanitization: Proxy 在轉發前清理 messages[] 中的 <details> 標籤

    流程:
      客戶端 → [X-Hermes-Session-Id: abc123] → Proxy → Gateway
      Gateway 從 state.db 載入歷史 → 合併請求 messages → 執行

    限制:
      - 需要 API key 認證才能使用 session 延續
      - Session ID 含控制字元會被拒絕（防注入）
      - 歷史由 Gateway 管理，Proxy 無法直接存取

### 6.2 Responses API — previous_response_id 模式

    機制: 請求體中傳入 previous_response_id 或 conversation 名稱
    歷史來源: response_store.db（Hermes 內部 SQLite，LRU 淘汰，最大 100 筆）
    歷史載入: _response_store.get(previous_response_id) → conversation_history
    歷史傳遞: Gateway 自動從儲存中重建 conversation_history

    流程:
      客戶端 → [previous_response_id: resp_xxx] → Gateway
      Gateway 從 response_store.db 載入歷史 → 合併 input → 執行
      執行後 → 新 response 存入 response_store.db
      若使用 conversation 名稱 → 自動更新名稱→ID 映射

    優先順序:
      conversation_history (請求體) > previous_response_id > 空歷史
      conversation 和 previous_response_id 互斥（同時提供會回傳 400）

### 6.3 兩模式歷史管理對比

    ┌──────────────────────┬──────────────────────────┬──────────────────────────┐
    │ 維度                 │ Chat Completions         │ Responses API            │
    ├──────────────────────┼──────────────────────────┼──────────────────────────┤
    │ 歷史載入             │ state.db (session)       │ response_store.db        │
    │ 歷史傳遞             │ 請求體 messages[]        │ previous_response_id 鏈   │
    │ 會話識別             │ X-Hermes-Session-Id      │ previous_response_id     │
    │ 歷史清理             │ Proxy 端 sanitize        │ Gateway 端自動管理       │
    │ 歷史容量             │ 取決於 LLM context       │ response_store 最大 100   │
    │ 截斷支援             │ 無（由 LLM 處理）        │ truncation="auto" (100則) │
    │ 歷史可檢索           │ 否（除非用 sessions API） │ 是（GET /v1/responses/{id}）│
    └──────────────────────┴──────────────────────────┴──────────────────────────┘

### 6.4 Proxy 在歷史管理中的角色

    Chat 模式:
      - 在轉發前對 messages[] 執行 sanitize_request_messages()
      - 將 <details type="tool_calls"> 轉換為自然語言描述
      - 結果截斷至 sanitization_result_max_length（預設 2000 字元）

    Responses 模式:
      - 不直接操作歷史（歷史由 Gateway 透過 response_store 管理）
      - 若客戶端傳入 conversation_history，可在轉發前清理
      - 若使用 previous_response_id，歷史由 Gateway 端自動重建
      - Proxy 只需透傳 previous_response_id 或 conversation 欄位

---

## 7. 資料持久化

### 7.1 ResponseStore 概述

    類別: ResponseStore (api_server.py 第 342 行)
    儲存引擎: SQLite3
    預設路徑: ~/.hermes/response_store.db
    後退路徑: :memory:（如果檔案路徑不可用）
    淘汰策略: LRU（Least Recently Used），最大容量 100 筆
    WAL 模式: 嘗試啟用 WAL，在 NFS/SMB/FUSE 上自動降級
    檔案權限: 0o600（僅擁有者可讀寫）

### 7.2 SQLite 資料表結構

    資料表: responses
      response_id  TEXT PRIMARY KEY  — response 的唯一識別碼
      data         TEXT NOT NULL     — 完整的 response JSON（含 output、usage 等）
      accessed_at  REAL NOT NULL     — 最後存取時間（用於 LRU 淘汰）

    資料表: conversations
      name       TEXT PRIMARY KEY  — 會話名稱（conversation 名稱）
      response_id TEXT NOT NULL     — 該會話最新的 response_id

### 7.3 儲存內容

    data 欄位（JSON）包含:
      {
        "response": {
          "id": "resp_...",
          "object": "response",
          "status": "completed",
          "created_at": 1234567890,
          "model": "hermes-agent",
          "output": [...],
          "usage": {"input_tokens": N, "output_tokens": N, "total_tokens": N}
        },
        "conversation_history": [...],
        "instructions": "string or null",
        "session_id": "uuid"
      }

    conversation_history 包含:
      - 所有歷史訊息（含工具調用和結果）
      - 格式: [{"role": "user"|"assistant"|"tool", "content": "...", ...}]

### 7.4 LRU 淘汰流程

    1. put() 時檢查總筆數
    2. 若超過 max_size（100），找出 accessed_at 最小的 N 筆
    3. 先刪除 conversations 中指向被淘汰 response_id 的映射
    4. 再刪除 responses 中的被淘汰記錄
    5. 一次性 commit

### 7.5 串流中的快照儲存

    串流模式下:
      - response.created 後立即儲存 in_progress 快照
      - 客戶端斷線時儲存 incomplete 快照
      - 正常完成時儲存 completed 快照
      - 快照使 GET /v1/responses/{id} 和 previous_response_id 鏈接在斷線後仍可恢復

### 7.6 與 Proxy 的關聯

    Proxy 不需直接操作 response_store.db
    - Responses 的歷史管理完全在 Gateway 端
    - Proxy 只需透傳 previous_response_id 和 conversation 欄位
    - GET/DELETE responses 端點直接透傳即可

---

## 8. Proxy 架構設計

### 8.1 建議的 Class 結構

```
HermesDualModeProxy (主類)
├── __init__(config: dict)
│   ├── _load_config()
│   ├── _build_port_map()
│   └── _init_http_session()
│
├── 路由層
│   ├── route_chat_completions(request) → ChatHandler
│   ├── route_responses(request) → ResponsesHandler
│   └── route_passthrough(request) → 通用透傳
│
├── ChatHandler (現行邏輯改造)
│   ├── handle_streaming(request, body)
│   ├── handle_non_streaming(request, body)
│   ├── sanitize_messages(messages)
│   └── transform_stream(upstream, model, ...)
│
├── ResponsesHandler (新增)
│   ├── handle_post(request, body)
│   ├── handle_get(response_id)
│   ├── handle_delete(response_id)
│   ├── handle_streaming(request, body)
│   └── handle_non_streaming(request, body)
│
├── 共用模組
│   ├── UpstreamRouter
│   │   ├── resolve_upstream(path)
│   │   └── forward_request(method, url, body, headers)
│   │
│   ├── HistorySanitizer
│   │   ├── sanitize_chat_messages(messages)
│   │   └── sanitize_responses_history(history)
│   │
│   └── SSETransformer
│       ├── transform_chat_sse(upstream, mode)
│       └── transform_responses_sse(upstream)
│
└── 工具模組
    ├── ToolCallBuffer (現有，僅 Chat 模式使用)
    └── ConfigManager
        ├── get_tool_mode()
        ├── get_sanitization_settings()
        └── get_port_map()
```

### 8.2 模式切換機制

    切換依據: 請求路徑
      /{port}/v1/chat/completions  → ChatHandler
      /{port}/v1/responses         → ResponsesHandler (POST)
      /{port}/v1/responses/{id}    → ResponsesHandler (GET/DELETE)
      其他                          → 通用透傳

    實作方式:
      在 FastAPI 路由層或 middleware 中根據 request.url.path 分派

    建議程式碼結構:
      @app.api_route("/{port_prefix}/{rest:path}", methods=["GET","POST","DELETE","PATCH"])
      async def dual_mode_proxy(request: Request, port_prefix: str, rest: str):
          if rest == "chat/completions":
              return await ChatHandler.handle(request, port_prefix)
          elif rest == "responses":
              return await ResponsesHandler.handle_post(request, port_prefix)
          elif rest.startswith("responses/"):
              response_id = rest.split("/")[1]
              method = request.method
              if method == "GET":
                  return await ResponsesHandler.handle_get(request, port_prefix, response_id)
              elif method == "DELETE":
                  return await ResponsesHandler.handle_delete(request, port_prefix, response_id)
          return await PassthroughHandler.handle(request, port_prefix, rest)

### 8.3 Handler 分工

    ChatHandler:
      - 負責現行所有 chat/completions 邏輯
      - 包含 history sanitization
      - 包含 SSE 工具卡片轉換（ToolCallBuffer + transform_stream）
      - 根據 tool_mode 配置決定轉換策略
      - 根據 User-Agent 決定 strip_details

    ResponsesHandler:
      - POST: 透傳請求體，透傳 SSE 串流
      - GET: 透傳 GET 請求到 Gateway
      - DELETE: 透傳 DELETE 請求到 Gateway
      - 初期不需 SSE 轉換（Responses 事件格式不同）
      - 未來可選：將 Responses 事件映射為工具卡片

    PassthroughHandler:
      - 處理非 chat/responses 的路徑（如 /v1/models, /health 等）
      - 直接轉發請求和回應

### 8.4 配置擴展

    在 config.yaml 中新增:

      # ── Dual Mode ──────────────────────────────────────────────
      # 啟用 Responses API 支援
      enable_responses_mode: true

      # Responses 模式的 SSE 處理模式
      # passthrough: 直接透傳（推薦初期使用）
      # map-cards: 將 Responses 事件映射為 <details> 工具卡片
      responses_sse_mode: "passthrough"

      # 是否對 conversation_history 欄位執行 sanitization
      sanitize_conversation_history: true

### 8.5 後ward 相容性

    - 現行 config.yaml 欄位保持不變
    - 新增欄位提供預設值（enable_responses_mode 預設 false）
    - 現行路由邏輯不受影響（新增分支，不修改現有邏輯）
    - PORT_MAP 不變（共用 upstream 路由表）

---

## 9. 實作優先順序

### P0 — 基礎路由與透傳（必須）

    [P0-1] 新增 Responses API 路由分派邏輯
          在 dual_mode_proxy 中識別 /v1/responses 路徑
          分派至 ResponsesHandler
    [P0-2] POST /v1/responses 非串流透傳
          完整透傳請求體和回應
    [P0-3] GET /v1/responses/{id} 透傳
    [P0-4] DELETE /v1/responses/{id} 透傳
    [P0-5] POST /v1/responses 串流透傳
          透傳 text/event-stream 回應

### P1 — 串流處理（重要）

    [P1-1] Responses SSE 串流正確透傳
          確保 text/event-stream 標頭和格式不被破壞
    [P1-2] 斷線處理
          客戶端斷線時正確關閉 upstream 連接
    [P1-3] conversation_history 欄位 sanitization
          若客戶端自訂 conversation_history，清理 <details> 標籤

### P2 — 功能增強（建議）

    [P2-1] Responses 模式工具卡片映射
          將 response.output_item.added/done 映射為 <details> 卡片
    [P2-2] 模式指示標頭
          在回應中標註 X-Hermes-Proxy-Mode: chat|responses
    [P2-3] 請求日誌增強
          記錄模式、工具數量、token 用量

### P3 — 進階功能（可選）

    [P3-1] Responses 模式的 auto_split 支援
    [P3-2] Idempotency-Key 在 Proxy 端的快取
    [P3-3] 兩種模式的統一監控儀表板

### P4 — 最佳化（未來）

    [P4-1] 連線池最佳化
    [P4-2] Responses 端點的壓縮支援
    [P4-3] 負載平衡與健康檢查整合

---

## 10. 關鍵檔案位置

### 10.1 Hermes Gateway 核心

    路徑: /usr/local/lib/hermes-agent/gateway/run.py
    大小: 約 942 KB
    說明: 主要路由定義、Gateway 啟動入口
    關鍵行:
      - Chat Completions 端點: 約第 21538-21652 行
      - Responses 端點: 約第 22617-22714 行
      - GET/DELETE responses: 約第 22716-22743 行
    注意: 行號可能隨版本更新變動，以 api_server.py 為準

### 10.2 API Server 平台適配器

    路徑: /usr/local/lib/hermes-agent/gateway/platforms/api_server.py
    大小: 約 189 KB (4259 行)
    說明: OpenAI 相容 API 的核心實作
    關鍵函式:
      - _handle_chat_completions(): 第 1683 行 — Chat 模式請求處理
      - _handle_responses(): 第 2754 行 — Responses 模式請求處理
      - _write_sse_responses(): 第 2158 行 — Responses SSE 串流輸出
      - ResponseStore: 第 342 行 — SQLite 儲存類別
      - 路由註冊: 第 4123-4126 行 — 端點註冊
    依賴: aiohttp.web

### 10.3 Proxy 現行程式碼

    路徑: /home/user/hermes_tool_filter/main.py
    大小: 1228 行
    說明: 現行 Proxy 主程式
    關鍵函式:
      - ToolCallBuffer: 第 591 行 — 工具調用緩衝
      - transform_stream(): 第 656 行 — SSE 串流轉換
      - get_session(): 第 981 行 — HTTP session 管理
      - proxy_with_transform(): 第 992 行 — 主要代理路由（含轉換）
      - proxy_default(): 第 1115 行 — 預設透傳路由
      - health(): 第 1208 行 — 健康檢查
    框架: FastAPI + aiohttp

### 10.4 配置檔

    路徑: /home/user/hermes_tool_filter/config.yaml
    大小: 30 行
    現行設定:
      bind_host: "0.0.0.0"
      bind_port: 9099
      tool_mode: "enhance-v2"
      auto_split_threshold: 0
      enable_history_sanitization: true
      sanitization_result_max_length: 2000
    建議新增:
      enable_responses_mode: true
      responses_sse_mode: "passthrough"
      sanitize_conversation_history: true

### 10.5 Systemd 服務

    路徑: /etc/systemd/system/hermes-tool-filter.service
    說明: 系統服務定義
    執行使用者: user
    工作目錄: /home/user/hermes_tool_filter
    啟動指令: /opt/hermes/hermes-agent/venv/bin/python3 -B main.py
    重啟策略: on-failure (5 秒後)
    前置條件: network-online.target, hermes-agent.service

### 10.6 其他相關檔案

    虛擬環境: /opt/hermes/hermes-agent/venv/
    Hermes 主目錄: ~/.hermes/
    Response 儲存: ~/.hermes/response_store.db
    狀態儲存: ~/.hermes/state.db
    日誌: journalctl -u hermes-tool-filter.service

---

## 附錄 A: 術語表

    SSE: Server-Sent Events，HTTP 單向串流協議
    LRU: Least Recently Used，最近最少使用淘汰策略
    WAL: Write-Ahead Logging，SQLite 的日誌模式
    Tool Call: LLM 產生的工具調用（function calling）
    Context 污染: 工具輸出以原始格式累積在對話歷史中，導致 prompt 膨脹
    Ephemeral System Prompt: 臨時系統提示，疊加在核心系統提示之上

## 附錄 B: 環境資訊

    作業系統: Linux 6.12.75+rpt-rpi-v8 (Raspberry Pi)
    Python: 3.11.15 (venv), pip → python3.13
    Hermes Profile: coder
    當前 Proxy 模式: chat_completions only (enhance-v2)
    目標模式: chat_completions + responses（雙模式）

## 附錄 C: 參考連結

    Hermes Agent 文件: https://hermes-agent.nousresearch.com/docs
    OpenAI Chat Completions API: https://platform.openai.com/docs/api-reference/chat
    OpenAI Responses API: https://platform.openai.com/docs/api-reference/responses

---

*本文檔為技術規格書，可直接作為開發實作的依據。*
*最後更新: 2026-06-08*
