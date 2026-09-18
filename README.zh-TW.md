# Hermes Tool Filter

接在 **Open WebUI** 與 **Hermes Gateway** 之間的 SSE 代理——兩個端點、兩種
context 模型，挑適合你聊天方式的那個。

[English](README.md) · 繁體中文

**v0.0.1**

---

## 兩個端點，挑你的 context 模型

| | `/v1/chat/completions` | `/v1/responses` |
|---|---|---|
| context 放在 | **客戶端**（Open WebUI） | **伺服器端**（Hermes session DB） |
| 每輪送出 | 完整歷史 | 只有最新一則訊息 |
| 編輯／重試舊訊息 | ✅ 可以 | ❌ 不行（server-side session） |
| Hermes 上下文壓縮 | ❌ 沒有 | ✅ 有——無縫，跟 TUI 一樣 |
| 長對話連續使用 | payload 會變大 | ✅ 連續、不膨脹 |
| 適合 | 日常聊天、改來改去 | 長 agent session、重工具工作 |

### `/v1/chat/completions` — 客戶端 context

- Open WebUI 存完整歷史，每輪都送回去。
- 可以**編輯、重試**任何訊息——適合日常來回對話。
- **沒有伺服器端壓縮**：OWUI 送什麼歷史，模型就看什麼，長對話 payload 會變大。
- **適合：** 想改來改去的中短日常對話。

### `/v1/responses` — 伺服器端 context

- 你只送最新一則訊息；Gateway 從 session DB 重新載入完整對話（含 tool calls）。
- **無縫的 Hermes 上下文壓縮**——session 存在伺服器端，Hermes 會像 TUI/CLI
  模式一樣自動壓縮。
- 對話可以**像 TUI 模式一樣連續跑**——沒有 payload 膨脹、不用管客戶端歷史。
- **不能編輯或重試舊訊息**——session 在伺服器端，改過去沒有意義；只能往後繼續。
- **適合：** 長、連續、重工具的 agent session。

---

## 運作方式

兩個端點修的是同一個核心問題：Hermes 內部 tool loop 是對的，但 Open WebUI
只把工具存成 assistant 文字裡的 HTML card。**下一輪**請求又把那段 HTML 當
普通 assistant 內容送回去 → 模型失憶，或開始模仿 `<details>`。

本代理做兩件事：

1. **出站** — 把 Hermes 的 `hermes.tool.progress` 轉成 Open WebUI tool card
2. **入站** — 在進 Gateway 前，把 card 改寫成模型能正確理解的 chat history
   （原生 `assistant` + `tool` role）

```
Open WebUI → hermes_tool_filter :9099 → Hermes Gateway :3000x → 模型
```

Open WebUI Base URL：`http://127.0.0.1:9099/<port>/v1`

### Session 續接（responses 專用）

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

## 安裝

```bash
git clone https://github.com/uraniumchonk/hermes-open-webui-adapter.git
cd hermes-open-webui-adapter
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
`patches/` 裡是**可選的自用 patch**（例如 tool-progress 帶更完整欄位、
api_server 允許 Markdown、Chat Completions 保留 `role=tool`、responses 的
session-id header）。**不是專案必要條件。**

若有套用，`hermes update` 後需重套；行號會飄，請當參考用，不保證每版 Hermes
都能 clean apply。

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
patches/                     # 可選自用 Hermes patch（見上）
config.yaml                  # 範例 config
requirements.txt
```

---

## License

MIT
