# Hermes Tool Filter — 工具歷史格式結構化重構計劃

> 目標：將目前 `[START_PREV_ACTION]` inline 文字替換，改為生出標準 OpenAI `tool` role message，
> 從根本上消滅上下文污染。最終可以廢除 `tool_hint.txt`。

---

## 一、背景：hermes_tool_filter 在做什麼

`hermes_tool_filter` 是架在 Open WebUI 和 Hermes Gateway（API Server）之間的透明代理：

```
Open WebUI ──→ hermes_tool_filter (:9099) ──→ Hermes Gateway (:30001/30002/30005)
```

它有兩個主要端點：
- `/v1/chat/completions` — 由 `completions_handler.py` 處理，走 SSE 串流模式
- `/v1/responses` — 由 `responses_handler.py` 處理，走 OpenAI Responses API 模式

核心問題：**工具呼叫結果的歷史記錄會污染上下文**。Open WebUI 用 `<details type="tool_calls">` 標籤把工具結果嵌在 assistant 的 content 裡，但這些格式再送回 LLM 時，模型會混淆：「這是我該輸出的格式嗎？還是歷史記錄？」— 導致模型模仿 `<details>` 格式、工具呼叫失敗。

---

## 二、現行方案：`[START_PREV_ACTION]` inline 替換

### 2.1 目前的工作流程

1. **sanitize_request_messages()**（`main.py` L253）在請求轉發給 Gateway 前掃描 messages
2. 對每條 `role=assistant` 的 content，用 regex 找出 `<details type="tool_calls">` 區塊
3. 呼叫 `sanitize_message_content()`（`tool_history_format.py` L377）→ `format_tool_history_block()`（L176）
4. 把 `<details>` 替換成以下格式，**塞回同一條 assistant message 的 content 裡**：

```
[START_PREV_ACTION]
[ACTION_TYPE]
web_search
[ACTION_ARG]
query: 台積電股價
[RESULT]
data[0].price: 1000.00
[END_PREV_ACTION]
```

5. 同時在最後一條 user message 後面 append `tool_hint.txt`，告訴模型「不要模仿這個格式」

### 2.2 現行方案的問題

| 問題 | 說明 |
|---|---|
| **語義混淆** | 模型看到的是 assistant role 輸出的奇怪文字，無法分辨「這是我的輸出」vs「這是系統餵給我的歷史」 |
| **模仿風險** | `tool_hint.txt` 是在語意層面下指令，不是結構層面的解決。長對話中 attention 稀釋後模型偶爾仍會模仿 |
| **自定義語法** | `[START_PREV_ACTION]` 是我們發明的格式，模型訓練資料中從未見過。需要模型額外學習理解 |
| **Prompt cache 不穩** | assistant content 持續增長（新工具結果一直 append），每次請求都破壞 prompt cache |

---

## 三、核心洞察：LLM 不吃 JSON，吃的是 `chat_template` 渲染後的純文字

這是主人（小夜）提出的關鍵洞察。

### 3.1 實際的資料流

```
[hermes_tool_filter]     [Gateway]              [vLLM serving backend]
messages (JSON)  ──→  messages (JSON)  ──→  chat_template 渲染 ──→  純文字 tokens ──→ LLM
```

**`chat_template` 是寫在模型 `tokenizer_config.json` 中的 Jinja2 模板**，例如 Qwen 系列：

```jinja2
{% for message in messages %}
<|im_start|>{{ message.role }}
{{ message.content }}<|im_end|>
{% endfor %}
```

關鍵：`message.role` 直接決定前面掛什麼 special token。模型在預訓練 / 微調時看過幾十億次這些 token 序列，天生就知道 `role="tool"` 的區塊是歷史記錄、不是該輸出的內容。

### 3.2 目前模型實際看到的文字

```
<|im_start|>assistant
讓我查一下。
[START_PREV_ACTION]
[ACTION_TYPE]
web_search
[RESULT]
晴天 25度
[END_PREV_ACTION]

今天晴天！<|im_end|>
<|im_start|>user
繼續<|im_end|>
```

模型理解：「assistant 輸出了一段奇怪的系統格式文字和結果。」— **完全丟失了結構資訊**。

### 3.3 改用 `role="tool"` 後模型看到的文字

```
<|im_start|>assistant
讓我查一下。<tool_call>{"name":"web_search","arguments":{"query":"天氣"}}</tool_call><|im_end|>
<|im_start|>tool
晴天，25度<|im_end|>
<|im_start|>assistant
今天晴天！<|im_end|>
<|im_start|>user
繼續<|im_end|>
```

模型瞬間理解：「上一輪 assistant 呼叫了 web_search，tool 回傳了結果，assistant 給了最終回覆。」**不需要任何 hint 或說明。**

---

## 四、Gateway 端相容性驗證：無需任何改動

已確認 Hermes Gateway (api_server.py) 原生支援 `role="tool"`：

### 4.1 History replay 路徑 (`gateway/run.py` L977-985)
```python
# Rich agent messages (tool_calls, tool results) must be passed through
# intact so the API sees valid assistant→tool sequences.
is_tool_message = role == "tool"
if has_tool_calls or has_tool_call_id or is_tool_message:
    agent_history.append(clean_msg)  # ← 直接透傳
```

### 4.2 Responses API 路徑 (`gateway/platforms/api_server.py` L4534-4539)
```python
elif role == "tool":
    items.append({
        "type": "function_call_output",
        "call_id": msg.get("tool_call_id", ""),
        "output": msg.get("content", ""),
    })
```

### 4.3 Session DB 寫入 (`gateway/session.py` L2581-2587)
```python
self._db.append_message(
    session_id=session_id,
    role=message.get("role", "unknown"),
    content=message.get("content"),
    tool_calls=message.get("tool_calls"),
    tool_call_id=message.get("tool_call_id"),
    ...
)
```

**結論：Gateway 三條路徑全部支援 `role="tool"`，零改動。**

---

## 五、實作計劃

### 5.1 核心改動：結構化拆分，而非文字替換

**當前邏輯**（`sanitize_request_messages`）：
```
For each assistant message:
    content = replace(<details> → [START_PREV_ACTION] inline text)
    msg["content"] = content    ← 同一條 message
```

**新邏輯**（結構化拆分）：
```
For each assistant message:
    1. Parse content: 用 regex 找出所有 <details> 區塊 → extract 工具資訊
    2. 移除 content 中的 <details>，保留純文字部分
    3. 根據 <details> 位置，把原 message 拆成多條:
       assistant(text) → tool(result) → assistant(text) → tool(result) → ...
    4. 取代原 messages array 中的該條目
```

**轉換範例**：

```python
# 輸入 (單條 assistant message)
{
    "role": "assistant",
    "content": "讓我查一下。\n\n<details type=\"tool_calls\" name=\"web_search\">\n<arguments>{\"query\":\"天氣\"}</arguments>\n<result>晴天，25度</result>\n</details>\n\n查完了！"
}

# 輸出 (拆成 3 條 messages)
[
    {
        "role": "assistant",
        "content": "讓我查一下。",
        "tool_calls": [{
            "id": "call_htf_a1b2c3",
            "type": "function",
            "function": {
                "name": "web_search",
                "arguments": "{\"query\":\"天氣\"}"
            }
        }]
    },
    {
        "role": "tool",
        "tool_call_id": "call_htf_a1b2c3",
        "content": "晴天，25度"
    },
    {
        "role": "assistant",
        "content": "查完了！"
    }
]
```

### 5.2 具體實作步驟

#### Step 1：新增 `tool_history_structured.py` — 結構化轉換引擎

在 `/home/thomas2018/hermes_tool_filter/` 下建立新檔案。

**職責**：
- `parse_assistant_content(content: str) -> list[Segment]`
  - 用 regex 解析 assistant content，找出所有 `<details>` 區塊
  - 回傳 segments 列表：`[{type: "text"|"tool", ...}]`
- `segments_to_messages(segments: list[Segment], call_id_prefix: str) -> list[dict]`
  - 把 segments 轉換成多條 OpenAI 格式 messages
  - 為每個 tool segment 生成唯一的 `call_id`（格式：`call_htf_{uuid_short}`）
- `sanitize_messages_structured(messages: list, config: dict) -> list`
  - 主入口：遍歷 messages，對每條 assistant 執行拆分
  - 已在 config 中新增 `tool_history_format: "structured"` 選項

**核心正則**（複用現有 `_extract_tool_info`）：
```python
# 從 tool_history_format.py 的 sanitize_message_content 中提取
DETAILS_PATTERN = re.compile(
    r'<details[^>]*type=tool_calls[^>]*>(.*?)</details>',
    re.DOTALL | re.IGNORECASE
)
```

**需處理的邊緣情況**：
- content 沒有 `<details>` → 直接回傳原 message（不變）
- 多個 `<details>` 串在一起 → 拆成 assistant → tool → assistant → tool → ...
- `<details>` 在 content 開頭或結尾（前後沒有文字）→ 不需要假的空 assistant message
- 純文字 assistant 後沒有 tool → 保留原樣
- `<details>` 中 arguments 解析失敗 → 回退為 `{}`

#### Step 2：修改 `sanitize_request_messages()` — 支援結構化模式

修改 `/home/thomas2018/hermes_tool_filter/main.py` L253 的 `sanitize_request_messages()`：

```python
def sanitize_request_messages(messages: list, model: str = "", hermes_sid: str = "") -> list:
    enabled, max_length, fmt = tool_history_format._get_sanitization_config(CONFIG)
    if not enabled:
        return messages

    if fmt == "structured":
        # 新的結構化轉換路徑
        from tool_history_structured import sanitize_messages_structured
        messages = sanitize_messages_structured(messages, CONFIG)
    
    elif fmt in ("flat", "legacy"):
        # 保留現有邏輯（向後相容）
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("content"):
                ...
    
    # tool_hint 注入：structured 模式下可以跳過（可選）
    if fmt == "structured":
        # structured 格式下模型天生理解 tool role，不需要 hint
        return messages
    
    # ... 現有的 tool_hint 注入邏輯 ...
```

#### Step 3：修改 Config 支援新格式選項

修改 `/home/thomas2018/hermes_tool_filter/config.yaml`：

```yaml
# History format:
#   structured — OpenAI native tool role messages (recommended, no pollution)
#   flat       — [START_PREV_ACTION] k:v format (current, prevents JSON pollution)
#   legacy     — natural language description (legacy)
tool_history_format: "structured"  # ← 預設改為 structured
```

修改 `config-zh.yaml` 同步。

#### Step 4：Completions 路徑測試驗證

`completions_handler.py` 中 `handle_completions_request()` 已在 L127 呼叫 `sanitize_request_messages()`，結構化轉換後會自動生效。

測試重點：
- 正常 multi-turn 對話（有歷史 `<details>` 的 assistant content）
- 純文字 assistant content（無 `<details>`）
- 極長工具結果（> 20k chars）的截斷
- 多 tool 並行的 assistant content

#### Step 5：Responses 路徑同步修改

修改 `/home/thomas2018/hermes_tool_filter/responses_handler.py` 的 `_inject_tool_context_into_input()`（約 L270）：

目前這個函數把 tools 結果打包成 `[START_PREV_ACTION]` 區塊注入到 user input 前面。

**新邏輯**：當 `tool_history_format` 為 `"structured"` 時，改為把 tools 結果作為多條 `role=tool` 的 message 插入到 `req_json["input"]` 陣列中（如果 input 是 messages 格式），或附加到 `req_json` 的 `instructions` 中宣告歷史工具結果（如果 input 是純文字）。

> 注意：Responses API 的 input 格式可能不同（純文字 vs dict list），需先確認上游實際傳來的格式。

#### Step 6：廢除 `tool_hint.txt`（可選）

`structured` 模式下不再需要 hint，因為模型從結構層面就能理解。但建議保留 `flat`/`legacy` 模式的 hint 機制不變，以維持向後相容。

### 5.3 不需要修改的部分

- `tool_history_format.py` — 保留 flat/legacy 格式實現，向後相容
- `completions_handler.py` — 不變，結構化轉換在 `sanitize_request_messages` 內部處理
- `comp_mode.py` — `[START_PREV_ACTION]` 壓縮邏輯保持不變（comp mode 是獨立的壓縮路徑）
- Gateway 端 — 零改動（已確認相容）

### 5.4 回退策略

如果 `structured` 模式出問題，只需在 config.yaml 中把 `tool_history_format` 改回 `"flat"` 即可立即回退到現行方案，因為所有現有邏輯都保留。

---

## 六、影響範圍總覽

| 檔案 | 操作 | 說明 |
|---|---|---|
| `tool_history_structured.py` | **新增** | 結構化轉換引擎（核心新模組） |
| `main.py` | **修改** | `sanitize_request_messages()` 增加 `structured` 分支 |
| `config.yaml` | **修改** | `tool_history_format` 新增 `structured` 選項、改預設值 |
| `config-zh.yaml` | **修改** | 同步 config.yaml |
| `responses_handler.py` | **修改** | `_inject_tool_context_into_input()` 支援 structured 模式 |
| `tool_history_format.py` | **不變** | 保留 flat/legacy 向後相容 |
| `completions_handler.py` | **不變** | 轉換由 sanitize 層處理 |
| `comp_mode.py` | **不變** | 獨立壓縮路徑 |

---

## 七、測試檢查清單

- [ ] `tool_history_format: "structured"` — 正常 multi-turn 對話，模型正確理解歷史工具結果
- [ ] `tool_history_format: "structured"` — assistant content 不含 `<details>`，原樣透傳
- [ ] `tool_history_format: "structured"` — 長工具結果截斷（`sanitization_result_max_length`）
- [ ] `tool_history_format: "structured"` — 多 `<details>` 在同一條 assistant content 中
- [ ] `tool_history_format: "flat"` — 向後相容，行為不變
- [ ] `tool_history_format: "legacy"` — 向後相容，行為不變
- [ ] Responses API 路徑 — tool context 正確注入
- [ ] Gateway session DB 寫入 — `role=tool` message 正確持久化
- [ ] Gateway history replay — `role=tool` message 正確回放
- [ ] 確認不再需要 `tool_hint.txt`（structured 模式下）
- [ ] model 不再出現模仿 `[START_PREV_ACTION]` 的行為
- [ ] model 不再出現模仿 `<details>` 標籤的行為

---

## 八、參考

- **Codebase**: `/home/thomas2018/hermes_tool_filter/`
- **核心檔案**:
  - `main.py` L253 — `sanitize_request_messages()` (completions 路徑入口)
  - `tool_history_format.py` L176 — `format_tool_history_block()` (現行 flat 格式產生器)
  - `tool_history_format.py` L377 — `sanitize_message_content()` (regex 替換邏輯)
  - `tool_history_format.py` L283 — `_extract_tool_info()` (從 `<details>` 提取工具名/參數/結果)
  - `responses_handler.py` L270 — `_inject_tool_context_into_input()` (responses 路徑)
  - `completions_handler.py` L127 — `handle_completions_request()` (呼叫 sanitize 的位置)
  - `config.yaml` — 設定檔
- **Gateway 端**（已確認無需修改）:
  - `~/.hermes/hermes-agent/gateway/run.py` L977 — history replay (role=tool 透傳)
  - `~/.hermes/hermes-agent/gateway/platforms/api_server.py` L4534 — responses API (function_call_output)
  - `~/.hermes/hermes-agent/gateway/session.py` L2581 — session DB (tool_call_id)
- **Hermes 自訂 Patch 管理**: `hermes-custom-patches` skill
- **系統服務**: `hermes-tool-filter.service`
