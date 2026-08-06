# Hermes Tool Filter

Hermes Gateway **Chat Completions**（`/v1/chat/completions`）透明 SSE 代理。
讓 Open WebUI 正確顯示 tool card，並把工具歷史安全餵回模型。

[English](README.md) · 繁體中文

> 只做 Chat Completions。不涵蓋 `/v1/responses`。

---

## 下載

Repo：https://github.com/uraniumchonk/hermes-open-webui-adapter

| 你要什麼 | 抓哪個 | 下載 / clone |
|----------|--------|----------------|
| **現在（只有 structured）** flat 已刪 | 分支 `main` 或 `dev-0.10+` | [ZIP main](https://github.com/uraniumchonk/hermes-open-webui-adapter/archive/refs/heads/main.zip) · [ZIP dev-0.10+](https://github.com/uraniumchonk/hermes-open-webui-adapter/archive/refs/heads/dev-0.10+.zip) |
| **還要 flat**（可切 `structured` / `flat`） | 分支 **`flat-history`**（= `877fdb7`；凍結，不再演進） | [ZIP flat-history](https://github.com/uraniumchonk/hermes-open-webui-adapter/archive/refs/heads/flat-history.zip) · [瀏覽分支](https://github.com/uraniumchonk/hermes-open-webui-adapter/tree/flat-history) |

同一點也有 tag `v2.0.0-dual-history`（內容同分支）：
[ZIP tag](https://github.com/uraniumchonk/hermes-open-webui-adapter/archive/refs/tags/v2.0.0-dual-history.zip)

```bash
# 現在（structured only）
git clone -b main https://github.com/uraniumchonk/hermes-open-webui-adapter.git

# 需要 flat
git clone -b flat-history https://github.com/uraniumchonk/hermes-open-webui-adapter.git
cd hermes-open-webui-adapter
# config.yaml → tool_history_format: "flat"
```

`61a58dc` 之後 main 只留 structured；現在碼寫 `flat` 只會 warning 後 fallback。

---

## 問題

Hermes 內部 tool loop 是完整的，但 SSE 只發 `hermes.tool.progress`。
Open WebUI 把工具存成 assistant 文字裡的 `<details>` → 下一輪當「模型自己講的」送回去 → 失憶或模仿 HTML。

---

## 架構

```
Open WebUI  →  hermes_tool_filter :9099  →  Hermes Gateway :3000x  →  模型
                 出站 enhance-v2
                 入站 history sanitize
```

Open WebUI Base URL：`http://127.0.0.1:9099/<port>/v1`

---

## 快速開始

```bash
pip install -r requirements.txt
# 改 config.yaml 的 upstreams
cd /path/to/hermes-agent
git apply /path/to/hermes_tool_filter/patches/api_server_chat_completions_all.patch
git apply /path/to/hermes_tool_filter/patches/prompt_builder_api_server_hint.patch
# 重啟 gateway 後
python main.py
```

---

## 兩個歷史範本

Open WebUI 存的是 `<details type="tool_calls">`。轉發 Gateway 前必須改寫。
**只有兩種寫法**——`tool_history_format`：

### 範本 1 — `structured`（native tool role，推薦 / 目前 runtime）

拆成真正的 OpenAI messages。`chat_template` 渲染成 `<|im_start|>tool`，
模型天生知道這是歷史，不是該輸出的格式。

**必裝 patch**：`api_server_chat_completions_all.patch`（否則 Gateway 丟掉 tool role）。

```json
[
  {
    "role": "assistant",
    "content": "讓我查一下。",
    "tool_calls": [{
      "id": "call_htf_a1b2",
      "type": "function",
      "function": {
        "name": "web_search",
        "arguments": "{\"query\": \"BTC price\"}"
      }
    }]
  },
  {
    "role": "tool",
    "tool_call_id": "call_htf_a1b2",
    "name": "web_search",
    "content": "{\"price\": 64000}"
  },
  {
    "role": "assistant",
    "content": "現在大約 64000。"
  }
]
```

```yaml
tool_history_format: "structured"
enable_history_sanitization: true
sanitization_result_max_length: 20000
```

### 範本 2 — `flat`（塞進同一條 assistant，舊 workaround）

不拆 role，結果改寫後**還是 assistant 長文**，靠 `tool_hint.txt` 叫模型別學。
不用 tool-role patch，但結構沒了，長對話仍可能污染。

**現在的 main 沒有 flat runtime**（`61a58dc` 刪了）。  
要用 flat → 分支 **[flat-history](https://github.com/uraniumchonk/hermes-open-webui-adapter/tree/flat-history)**  
（[ZIP](https://github.com/uraniumchonk/hermes-open-webui-adapter/archive/refs/heads/flat-history.zip)，= `877fdb7`）。

```text
讓我查一下。

[START_PREV_ACTION]
[ACTION_TYPE]
web_search
[ACTION_ARG]
query: BTC price
[RESULT]
price: 64000
[END_PREV_ACTION]

現在大約 64000。
```

```yaml
tool_history_format: "flat"
enable_history_sanitization: true
sanitization_result_max_length: 20000
tool_usage_hint_file: "tool_hint.txt"
```

| | structured | flat |
|--|------------|------|
| 模型看到的 role | `assistant` + `tool` | 只有 `assistant` |
| 防污染 | 結構層 | hint（不穩） |
| Gateway patch | tool-role + result **必裝** | 有 SSE result 即可 |
| 現況 | **唯一 runtime** | 只在 git 歷史 |

出站都是 enhance-v2 的 `<details>` card；差在**下一輪入站**怎麼還原。

---

## Config（需改在上）

穩定對照：`git show 877fdb7:config.yaml`

```yaml
upstreams:
  "30001": "http://127.0.0.1:30001"
  "30002": "http://127.0.0.1:30002"
  "30005": "http://127.0.0.1:30005"

tool_history_format: "structured"
enable_history_sanitization: true
sanitization_result_max_length: 20000
tool_mode: "enhance-v2"

bind_host: "0.0.0.0"
bind_port: 9099
auto_split_threshold: 0
compression_mode: "disabled"
session_isolation_mode: "disabled"
```

完整註解：`config.yaml` / `config-zh.yaml`。

Gateway：`API_SERVER_ENABLED=true`、`API_SERVER_PORT` 對上 key、`API_SERVER_KEY`。

---

## Hermes patch

`patches/` 只有兩份：

| 檔案 | 用途 |
|------|------|
| `api_server_chat_completions_all.patch` | SSE args/result + 保留 tool role |
| `prompt_builder_api_server_hint.patch` | 允許 Markdown |

```bash
cd ~/.hermes/hermes-agent
git apply .../patches/api_server_chat_completions_all.patch
git apply .../patches/prompt_builder_api_server_hint.patch
# 三個 grep 都應 >0：result_str / OpenAI-native tool result / Markdown is allowed
```

`hermes update` 後重套。

---

## 疑難

| 現象 | 處理 |
|------|------|
| 沒 card | enhance-v2 + 走 filter port |
| 空 result | 重套 api_server patch |
| structured 失憶 | tool-role 沒套上 |
| 學 details / START_PREV_ACTION | 用 structured，別裸跑 flat |

---

## License

MIT
