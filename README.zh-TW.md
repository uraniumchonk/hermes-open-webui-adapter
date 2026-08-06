# Hermes Tool Filter

接在 **Open WebUI** 與 **Hermes Gateway** 之間的 SSE 代理（只做 `/v1/chat/completions`）。

[English](README.md) · 繁體中文

---

## 問題

Hermes 內部 tool loop 是對的，但 Open WebUI 只把工具存成 assistant 文字裡的 HTML card。
**下一輪**請求又把那段 HTML 當普通 assistant 內容送回去 → 模型失憶，或開始模仿 `<details>`。

本代理做兩件事：

1. **出站** — 把 Hermes 的 `hermes.tool.progress` 轉成 Open WebUI tool card  
2. **入站** — 在進 Gateway 前，把 card 改寫成模型能正確理解的 chat history

```
Open WebUI → hermes_tool_filter :9099 → Hermes Gateway :3000x → 模型
```

Open WebUI Base URL：`http://127.0.0.1:9099/<port>/v1`

---

## 下一輪 payload 長怎樣

Open WebUI 存下來大致是這樣（簡化）：

```json
{
  "role": "assistant",
  "content": "讓我查一下。\n\n<details type=\"tool_calls\" done=\"true\" name=\"web_search\">\n<summary>web_search</summary>\n<arguments>{\"query\": \"BTC price\"}</arguments>\n<result>{\"price\": 64000}</result>\n</details>\n\n大約 64000。"
}
```

沒過 filter 時，模型下一輪看到的就是整段字串。  
過 filter 後會改寫歷史。**兩種範本：**

### `structured` — 原生 tool role（目前 `main`）

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
    "content": "大約 64000。"
  }
]
```

需要 Hermes patch，Gateway 才會保留 `role=tool` 與 `tool_calls`。

### `flat` — 仍塞同一條 assistant（舊分支）

```json
{
  "role": "assistant",
  "content": "讓我查一下。\n\n[START_PREV_ACTION]\n[ACTION_TYPE]\nweb_search\n[ACTION_ARG]\nquery: BTC price\n[RESULT]\nprice: 64000\n[END_PREV_ACTION]\n\n大約 64000。"
}
```

不用 tool-role patch；靠 hint 防止模型模仿這段格式。

| | `structured` | `flat` |
|--|--------------|--------|
| 模型看到的 role | `assistant` + `tool` | 只有 `assistant` |
| 防污染 | 結構層 | 靠 hint |
| 分支 | **`main`**（唯一選項） | **`flat-history`**（凍結） |

---

## 下載

https://github.com/uraniumchonk/hermes-open-webui-adapter

| 需求 | 分支 | 連結 |
|------|------|------|
| 現在（只有 structured） | `main` | [ZIP](https://github.com/uraniumchonk/hermes-open-webui-adapter/archive/refs/heads/main.zip) |
| 要 `flat` 可切換 | `flat-history` | [ZIP](https://github.com/uraniumchonk/hermes-open-webui-adapter/archive/refs/heads/flat-history.zip) · [瀏覽](https://github.com/uraniumchonk/hermes-open-webui-adapter/tree/flat-history) |

```bash
git clone -b main https://github.com/uraniumchonk/hermes-open-webui-adapter.git
# 或：git clone -b flat-history ...
```

tag `v2.0.0-dual-history` 與 `flat-history` 相同（= `877fdb7`）。

---

## 安裝

```bash
pip install -r requirements.txt
# 改 config.yaml 的 upstreams

cd /path/to/hermes-agent
git apply /path/to/patches/api_server_chat_completions_all.patch
git apply /path/to/patches/prompt_builder_api_server_hint.patch
# 重啟 gateway

python main.py
```

**Patch**（`patches/`）：

- `api_server_chat_completions_all.patch` — SSE 帶 `arguments`/`result`，並保留 tool role  
- `prompt_builder_api_server_hint.patch` — api_server 允許 Markdown  

每次 `hermes update` 後重套。

**最小 config：**

```yaml
upstreams:
  "30001": "http://127.0.0.1:30001"

tool_mode: "enhance-v2"
enable_history_sanitization: true
sanitization_result_max_length: 20000
# main 固定 structured；flat 只在 flat-history 分支
```

Gateway `.env`：`API_SERVER_ENABLED=true`，`API_SERVER_PORT` 對上 upstream key，`API_SERVER_KEY=...`。

---

## License

MIT
