# Hermes Tool Filter

接在 **Open WebUI** 與 **Hermes Gateway** 之間的 SSE 代理（`/v1/chat/completions` 與 `/v1/responses`）。

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

### `flat` — 仍塞同一條 assistant（舊，凍結）

舊的 `flat` 範本（單條 assistant 字串 + `[START_PREV_ACTION]` hint）在已刪除的
`flat-history` 分支。`main` 只出 `structured`。

---

## `/v1/responses` — session 續接

Open WebUI 的 Responses 模式會在輪次之間丟掉 tool item，agent 因此失憶。
filter 用輕量 session marker 解決：

1. **第一輪** — 請求沒有 marker → 原樣轉發；回應在第一則 assistant 訊息末尾
   附加可見的 session 標籤：
   ````
   ```
   <!--hermes-sid:<id>-->
   ```
   ````
2. **之後** — filter 讀那一顆 marker（正向掃、early exit，中間的超大歷史完全不碰），
   把請求改寫成 `[最後一個 user]` + `X-Hermes-Session-Id` header，
   Gateway 從 session DB 重新載入完整對話（含 tool calls）。

marker 不會到模型（filter 丟掉 payload 歷史、Gateway 端再剝離殘留）。
每個 chat 只有一顆，在第一則 assistant 訊息。

---

## 下載

https://github.com/uraniumchonk/hermes-open-webui-adapter

```bash
git clone https://github.com/uraniumchonk/hermes-open-webui-adapter.git
# 或：https://github.com/uraniumchonk/hermes-open-webui-adapter/archive/refs/heads/main.zip
```

---

## 專案結構

```
main.py                      # 入口：proxy routing、串流、health
completions_handler.py       # /v1/chat/completions — tool card enhance + sanitize
responses_handler.py         # /v1/responses — session 續接 + tool results
responses_session.py         # sid marker 提取/注入（responses 專用）
tool_history_format.py       # tool card → 模型安全 history（structured）
tool_history_structured.py   # structured sanitizer（原生 tool role）
native_tool_context.py       # 原生 tool-context 注入
special_tags.py              # neutralize tool result 內的特殊標籤
special_tags.json            # 標籤清單（data）
extract_tags.py              # 一次性標籤產生器（升級 model/OWUI 後重跑）
comp_mode.py                 # 可選 tool-result 壓縮
patches/                     # 可選自用 Hermes patch（見下）
config.yaml                  # 範例 config
requirements.txt
```

---

## 安裝

```bash
pip install -r requirements.txt
# 改 config.yaml 的 upstreams
python main.py
```

```yaml
upstreams:
  "30001": "http://127.0.0.1:30001"

tool_mode: "enhance-v2"
enable_history_sanitization: true
sanitization_result_max_length: 20000
```

Gateway `.env`：`API_SERVER_ENABLED=true`，`API_SERVER_PORT` 對上 upstream key，`API_SERVER_KEY=...`。

### 可選：本機 Hermes patch

不改 Hermes 也能跑這個 proxy。  
`patches/` 裡是**可選的自用 patch**（例如 tool-progress 帶更完整欄位、api_server 允許 Markdown、
Chat Completions 保留 `role=tool`）。**不是專案必要條件。**

若有套用，`hermes update` 後需重套；行號會飄，請當參考用，不保證每版 Hermes 都能 clean apply。

---

## License

MIT
