# Hermes Tool Filter

Transparent SSE proxy for Hermes Gateway **Chat Completions** (`/v1/chat/completions`).
Renders tool cards in Open WebUI and feeds tool history back to the model without pollution.

English · [繁體中文](README.zh-TW.md)

> Chat Completions only. `/v1/responses` is out of scope.

---

## Download

Repo: https://github.com/uraniumchonk/hermes-open-webui-adapter

| 你要什麼 | 抓哪個 | 下載 / clone |
|----------|--------|----------------|
| **現在（structured only）** flat 已刪 | 分支 `main` 或 `dev-0.10+` | [ZIP main](https://github.com/uraniumchonk/hermes-open-webui-adapter/archive/refs/heads/main.zip) · [ZIP dev-0.10+](https://github.com/uraniumchonk/hermes-open-webui-adapter/archive/refs/heads/dev-0.10+.zip) |
| **還要 flat**（可切 `structured` / `flat`） | 分支 **`flat-history`**（= `877fdb7`，刪 flat 前最後狀態；凍結，不再演進） | [ZIP flat-history](https://github.com/uraniumchonk/hermes-open-webui-adapter/archive/refs/heads/flat-history.zip) · [瀏覽分支](https://github.com/uraniumchonk/hermes-open-webui-adapter/tree/flat-history) |

同一點也打了 tag `v2.0.0-dual-history`（內容同 `flat-history`）：
[ZIP tag](https://github.com/uraniumchonk/hermes-open-webui-adapter/archive/refs/tags/v2.0.0-dual-history.zip)

```bash
# 現在（structured only）
git clone -b main https://github.com/uraniumchonk/hermes-open-webui-adapter.git

# 需要 flat
git clone -b flat-history https://github.com/uraniumchonk/hermes-open-webui-adapter.git
cd hermes-open-webui-adapter
# config.yaml → tool_history_format: "flat"   # 或 "structured"
```

`61a58dc` 起 main runtime 只留 structured；現在碼寫 `flat` 會 warning 後 fallback。

---

## Problem

Hermes runs a full tool loop internally, but SSE only emits `hermes.tool.progress`.
Open WebUI stores tools as HTML `<details>` inside assistant text. Next turn that HTML
is sent back as “assistant output” → model amnesia or mimicry.

---

## Architecture

```
Open WebUI  →  hermes_tool_filter :9099  →  Hermes Gateway :3000x  →  model
                 enhance-v2 out
                 history sanitize in
```

Open WebUI Base URL: `http://127.0.0.1:9099/<port>/v1`

---

## Quick start

```bash
pip install -r requirements.txt
# edit upstreams in config.yaml
cd /path/to/hermes-agent
git apply /path/to/hermes_tool_filter/patches/api_server_chat_completions_all.patch
git apply /path/to/hermes_tool_filter/patches/prompt_builder_api_server_hint.patch
# restart gateway, then:
python main.py
```

---

## 兩個歷史範本（History templates）

Open WebUI 存的是 `<details type="tool_calls">`。轉發給 Gateway 前，filter 必須改寫。
**只有兩種寫法**——config 鍵 `tool_history_format`：

### 範本 1 — `structured`（native tool role，推薦 / 目前 runtime）

拆成真正的 OpenAI messages。模型經 `chat_template` 看到的是 `<|im_start|>tool`，
不是「assistant 自己講了一段怪文字」。

**需要 Hermes patch**：`api_server_chat_completions_all.patch`（保留 `role=tool` + `tool_calls`）。

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

### 範本 2 — `flat`（塞進同一條 assistant content，舊 workaround）

不拆 role，把結果改寫後**仍放在 assistant 文字裡**，外加 hint 叫模型別模仿。
不需 tool-role patch，但結構資訊丟失，長對話仍可能污染。

**現在的 main 已沒有 flat runtime**（`61a58dc` 刪除）。  
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
tool_usage_hint_file: "tool_hint.txt"   # flat 才需要
```

| | structured | flat |
|--|------------|------|
| 模型看到的 role | `assistant` + `tool` | 只有 `assistant` 長文 |
| 污染 / 模仿 | 結構層隔開 | 靠 hint，仍可能學 |
| Gateway patch | **必裝** tool-role + result | 只要 SSE result 即可 |
| 現況 | **唯一 runtime 路徑** | git 歷史（≤877fdb7） |

出站（Gateway→UI）兩邊相同：enhance-v2 注入 `<details>` tool card。差別只在**下一輪入站**怎麼還原歷史。

---

## Config（需改在上）

穩定對照：`git show 877fdb7:config.yaml`

```yaml
# 必改
upstreams:
  "30001": "http://127.0.0.1:30001"
  "30002": "http://127.0.0.1:30002"
  "30005": "http://127.0.0.1:30005"

# 常用 — 選一個歷史範本（見上）
tool_history_format: "structured"
enable_history_sanitization: true
sanitization_result_max_length: 20000
tool_mode: "enhance-v2"

# 很少改
bind_host: "0.0.0.0"
bind_port: 9099
auto_split_threshold: 0
compression_mode: "disabled"
session_isolation_mode: "disabled"
```

完整註解版：`config.yaml` / `config-zh.yaml`。

Env：`TOOL_MODE` `BIND_PORT` `BIND_HOST` `AUTO_SPLIT_THRESHOLD`。

Gateway `.env`：`API_SERVER_ENABLED=true`、`API_SERVER_PORT` 對上 upstreams key、`API_SERVER_KEY=...`。

---

## Hermes patches

全部在 `patches/`（只這兩份）：

| Patch | 用途 |
|-------|------|
| `api_server_chat_completions_all.patch` | SSE `arguments`+`result`；Chat Completions 保留 tool role |
| `prompt_builder_api_server_hint.patch` | API server 允許 Markdown |

```bash
cd ~/.hermes/hermes-agent
git apply /path/to/hermes_tool_filter/patches/api_server_chat_completions_all.patch
git apply /path/to/hermes_tool_filter/patches/prompt_builder_api_server_hint.patch

grep -c '"result": result_str' gateway/platforms/api_server.py       # >0
grep -c 'OpenAI-native tool result' gateway/platforms/api_server.py  # >0
grep -c 'Markdown is allowed' agent/prompt_builder.py                # >0
```

`hermes update` 後重套。行號飄了就從 working tree 重匯 diff 蓋掉 patch 檔。

---

## systemd

```ini
[Unit]
Description=Hermes Tool Filter
After=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/hermes_tool_filter
ExecStart=/path/to/hermes_tool_filter/venv/bin/python main.py
Restart=always
MemoryMax=1G
MemorySwapMax=256M

[Install]
WantedBy=multi-user.target
```

---

## Troubleshooting

| 現象 | 處理 |
|------|------|
| 沒 tool card | `tool_mode: enhance-v2`；URL 要走 filter port |
| card 空 result | 重套 api_server patch |
| structured 下一輪失憶 | patch 沒保留 tool role；`grep OpenAI-native tool result` |
| 模型學 `<details>` / `START_PREV_ACTION` | 用 structured；別回 flat 又不加 hint |
| update 後壞掉 | 兩份 patch 重套 + 重啟 gateway/filter |

---

## License

MIT
