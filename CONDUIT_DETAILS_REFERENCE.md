# Conduit APP `<details>` 標籤渲染參考

> **目的**：記錄 Conduit APP 如何解析與渲染 `<details>` 標籤，特別是 `tool_calls` 類型。
> 這份文件讓 hermes_tool_filter 等中介服務能正確生成 Conduit APP 期望的格式。
> 
> **最後更新**：2026-06-05
> **Conduit APP 版本**：v3.1.6 (~74aa7de)

---

## 1. 核心原則

### ⚠️ 關鍵規則

1. **`arguments` 和 `result` 必須是 `<details>` 標籤的屬性（attribute）**，不是子標籤（child element）
2. 屬性值經過 **JSON 編碼 → HTML 轉義** 雙重編碼
3. `done` 屬性只有字串 `"true"` 才算完成，其他任何值（包括 `"false"`）都視為 pending
4. 不完整的 `<details>`（缺少 `</details>`）會被隱藏，不渲染

### ❌ 常見錯誤

```html
<!-- ❌ 錯誤：arguments 作為子標籤 -->
<details type="tool_calls" done="true" name="terminal">
<summary>Done</summary>
<arguments>echo test</arguments>
<result>output</result>
</details>

<!-- ✅ 正確：arguments 作為屬性 -->
<details type="tool_calls" done="true" name="terminal" arguments="{&quot;input&quot;:&quot;echo test&quot;}" result="&quot;output&quot;">
<summary>Done</summary>
</details>
```

---

## 2. 完整屬性規格

| 屬性 | 類型 | 必填 | 說明 |
|------|------|------|------|
| `type` | String | ✅ | `tool_calls` / `reasoning` / `code_interpreter` |
| `name` | String | tool_calls 必填 | 工具名稱（如 "search", "terminal"） |
| `done` | String | ✅ | `"true"` = 完成, `"false"` 或其他 = pending |
| `id` | String | 選填 | 工具調用 ID（用於追蹤） |
| `arguments` | String | 選填 | 工具參數（JSON 編碼 + HTML 轉義） |
| `result` | String | 選填 | 工具結果（JSON 編碼 + HTML 轉義） |
| `files` | String | 選填 | 檔案/圖片 URL 陣列（JSON + HTML 轉義） |
| `embeds` | String | 選填 | 嵌入內容來源 URL 陣列（JSON + HTML 轉義） |
| `duration` | String | reasoning 選填 | 推理耗時（秒數，如 `"5"`） |
| `body_markdown` | String | 選填 | 內部 markdown 內容 |

---

## 3. 編碼規則

### 3.1 雙重編碼流程

```python
import json
import html

def encode_detail_attribute(value):
    """
    將值編碼為 <details> 屬性。
    順序：JSON-encode → HTML-escape
    """
    if not value:
        return ""
    # 第一步：JSON 編碼
    json_str = json.dumps(value, ensure_ascii=False)
    # 第二步：HTML 轉義
    return html.escape(json_str, quote=True)
```

### 3.2 HTML 轉義對應表

| 原始字元 | HTML Entity |
|---------|-------------|
| `"` | `&quot;` |
| `'` | `&apos;` |
| `<` | `&lt;` |
| `>` | `&gt;` |
| `&` | `&amp;` |

### 3.3 編碼範例

```python
# 輸入：{"command": "echo 'hello'"}
# 第一步（JSON）：{"command": "echo 'hello'"}
# 第二步（HTML）：{&quot;command&quot;: &quot;echo 'hello'&quot;}

# 輸入：simple string
# 第一步（JSON）："simple string"
# 第二步（HTML）：&quot;simple string&quot;
```

---

## 4. 完整格式範例

### 4.1 Tool Calls — 執行中（Pending）

```html
<details type="tool_calls" done="false" name="search">
<summary>Executing...</summary>
</details>
```

### 4.2 Tool Calls — 已完成（帶 arguments + result）

```html
<details type="tool_calls" done="true" name="search" arguments="{&quot;q&quot;:&quot;cats&quot;}" result="&quot;done&quot;">
<summary>Done</summary>
</details>
```

### 4.3 Tool Calls — 已完成（僅 result）

```html
<details type="tool_calls" done="true" name="browser" result="&quot;two&quot;">
<summary>Done</summary>
</details>
```

### 4.4 Tool Calls — 帶 embeds

```html
<details type="tool_calls" done="true" name="browser" embeds="[&quot;https://example.com/embed&quot;]">
<summary>Done</summary>
</details>
```

### 4.5 Tool Calls — 帶 files（圖片）

```html
<details type="tool_calls" done="true" name="upload" files="[&quot;a.txt&quot;,&quot;b.txt&quot;]">
<summary>Done</summary>
</details>
```

### 4.6 Reasoning — 思考中

```html
<details type="reasoning" done="false">
<summary>Thinking…</summary>
推理內容...
</details>
```

### 4.7 Reasoning — 已完成（帶耗時）

```html
<details type="reasoning" done="true" duration="5">
<summary>Thought for 5 seconds</summary>
> 推理內容...
</details>
```

### 4.8 群組多個 Tool Calls

當連續多個 `type="tool_calls"` 出現時，Conduit APP 會自動群組：

```html
<details type="tool_calls" done="true" name="terminal" arguments="{&quot;input&quot;:&quot;ls&quot;}">
<summary>Done</summary>
</details>
<details type="tool_calls" done="true" name="read_file" arguments="{&quot;input&quot;:&quot;file.py&quot;}">
<summary>Done</summary>
</details>
```

渲染為：
> **Explored terminal, read_file** ✓
> - View Result from terminal
> - View Result from read_file

---

## 5. 渲染行為

### 5.1 狀態對應

| `done` 屬性 | 狀態 | 標題 | 圖示 | 展開行為 |
|-------------|------|------|------|----------|
| `done="false"` | Pending | "Executing {name}…" | 旋轉 Spinner | BottomSheet（shimmer 動畫） |
| `done="true"` | Completed | "View Result from {name}" | 綠色 ✓ | BottomSheet |
| 無 `done` 屬性 | N/A | 依 summary 顯示 | 依類型 | 依類型 |
| 不完整的標籤 | Hidden | 不顯示 | — | 不顯示 |

### 5.2 Arguments 顯示邏輯

Conduit APP 對 `arguments` 的處理：

1. **HTML 解碼**：`_decodeDetailAttribute(attributes['arguments'])`
2. **JSON 解析**：`_parseDetailJsonString()`（支援雙層解碼）
3. **分支顯示**：
   - 解析為 **Map** → 每個 key-value 顯示為 `key: value` 列表
   - 解析為 **非 Map**（字串/陣列等）→ 格式化為 JSON code block

### 5.3 Result 顯示邏輯

1. **HTML 解碼**
2. **JSON 解析**
3. **分支顯示**：
   - 解析為 **Map/List** → JSON code block
   - 解析為 **其他** → 純文字顯示

---

## 6. Python 工具函數

### 6.1 編碼函數

```python
import json
import html

def encode_detail_attribute(value) -> str:
    """將值編碼為 <details> 屬性。"""
    if not value:
        return ""
    json_str = json.dumps(value, ensure_ascii=False)
    return html.escape(json_str, quote=True)
```

### 6.2 建立完成的工具標籤

```python
def build_completion_details(tool_name: str, label: str = "", result: str = "") -> str:
    """建立完整的 <details> 標籤供 Conduit APP 渲染。"""
    safe_name = html.escape(tool_name) if tool_name else "unknown"
    
    attrs = f'type="tool_calls" done="true" name="{safe_name}"'
    
    if label:
        args_dict = {"input": label}
        attrs += f' arguments="{encode_detail_attribute(args_dict)}"'
    
    if result:
        truncated = result[:5000] + ("..." if len(result) > 5000 else "")
        attrs += f' result="{encode_detail_attribute(truncated)}"'
    
    # **重要：前面加 \n\n 確保 <details> 在行首**（Conduit BlockSyntax 要求）
    return f'\n\n<details {attrs}>\n<summary>Done</summary>\n</details>\n'
```

### 6.3 建立執行中的工具標籤

```python
def build_running_details(tool_call_id: str, tool_name: str, emoji: str, label: str) -> str:
    """建立執行中的 <details> 標籤。"""
    safe_name = html.escape(tool_name)
    return (
        f'<details type="tool_calls" done="false" id="{tool_call_id}" '
        f'name="{safe_name}">\n'
        f'<summary>{emoji} Running... {html.escape(label)}</summary>\n'
        f'</details>\n'
    )
```

---

## 7. 資料流

```
Hermes Gateway
    │
    │  hermes.tool.progress (running/completed)
    │  <details done="false"> (running 時)
    │
    ▼
hermes_tool_filter (enhance 模式)
    │
    │  1. enhance: 過濾 done=false + 在 completed 時注入 done=true 標籤
    │  2. enhance-v2: 不發 running 卡片 + 只在 completed 時注入 done=true
    │                 + 過濾所有 <details> 標籤（包括 Gateway 發送的）
    │                 + 標籤前加 \n\n 確保在行首
    │  3. arguments/result 放在屬性中，非子標籤
    │
    ▼
Conduit APP SSE Parser
    │
    │  1. DetailsBlockSyntax 解析屬性
    │  2. HtmlUnescape() 解碼屬性值
    │  3. json.decode() 解析 JSON
    │
    ▼
Markdown Compile Service
    │
    │  1. _compileToolCallData() 提取 arguments/result
    │  2. 分為 argumentEntries / argumentsCode / resultCode
    │
    ▼
UI Rendering
    │
    │  - Pending: "Executing {name}…" + Spinner
    │  - Completed: "View Result from {name}" + ✓
    │  - 展開後顯示 Input/Output 區塊
    │
    ▼
Conduit APP UI
```

---

## 8. 測試用例（來自 Conduit APP）

```dart
// 測試用例 1：帶 arguments + result
'<details type="tool_calls" done="true" name="search" arguments="{&quot;q&quot;:&quot;cats&quot;}" result="&quot;done&quot;">'

// 測試用例 2：僅 arguments
'<details type="tool_calls" done="true" name="search" arguments="{&quot;q&quot;:&quot;cats&quot;}">'

// 測試用例 3：帶巢狀 JSON
'<details type="tool_calls" done="true" name="run_command" arguments="&quot;{&quot;command&quot;:&quot;python&quot;}&quot;">'
```

---

## 9. 注意事項

1. **結果截斷**：result 超過 5000 字元時截斷並加上 `...`
2. **XSS 防護**：所有屬性值都必須經過 HTML 轉義
3. **群組規則**：只有 `type="tool_calls"` 會被自動群組
4. **OpenWebUI 兼容**：當 `result` 屬性為空時，Conduit 會將 body 內容移動到 `result` 屬性
5. **SSE 串流**：不完整的 `<details>`（缺少 `</details>`）在串流期間不會渲染

---

## 10. 相關檔案

| 檔案 | 職責 |
|------|------|
| `details_block_syntax.dart` | Markdown 層解析 `<details>` 為 AST 節點 |
| `markdown_compile_service.dart` | 將屬性轉為 `CompiledMarkdownDetailsData` |
| `compiled_markdown_document.dart` | 數據模型定義 |
| `details_block_widget.dart` | 單一卡片的 UI 渲染 |
| `details_group_widget.dart` | 多個 tool_calls 的群組渲染 |
| `streaming_markdown_widget.dart` | 串流期間的 markdown 渲染 |

---

## 11. 版本記錄

| 日期 | 變更 |
|------|------|
| 2026-06-05 | 初始版本，記錄 Conduit APP v3.1.6 的渲染規格 |
| 2026-06-04 | commit `2d1a834` 引入回歸（arguments 改為子標籤） |
| 2026-06-05 | 修復：恢復為屬性格式 |
| 2026-06-05 | 新增 enhance-v2 模式：不發送 running 卡片，只在 completed 時發送 |
| 2026-06-05 | 修復：`<details>` 標籤前加 `\n\n` 確保在行首（Conduit BlockSyntax 要求） |
