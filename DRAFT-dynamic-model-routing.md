# 單一入口動態模型路由方案

## 問題

目前 Open WebUI 需要為每個 Hermes Gateway profile 配置一個獨立的 API 連線：

```
http://127.0.0.1:9099/30000/v1  (chat)
http://127.0.0.1:9099/30001/v1  (coder)
http://127.0.0.1:9099/30002/v1  (analyst)
http://127.0.0.1:9099/30003/v1  (trader)
```

每個連線都要填入對應的 API key，管理麻煩。

## 目標

讓 Open WebUI 只需配置**一個** API 連線：

```
Base URL: http://127.0.0.1:9099/v1
API Key: (統一 key 或留空)
```

Tool Filter 負責：
1. 聚合所有上游的模型列表，回傳給前端
2. 根據請求中的 `model` 名稱，路由到正確的 port
3. 使用該 port 對應的 API key 轉發請求

## 架構設計

### 1. Config 變更

**upstreams** 從純字串改為物件格式，支援每個 port 有自己的 API key：

```yaml
upstreams:
  "30000":
    url: "http://127.0.0.1:30000"
    api_key: "***"
  "30001":
    url: "http://127.0.0.1:30001"
    api_key: "***"
  "30002":
    url: "http://127.0.0.1:30002"
    api_key: "***"
  "30003":
    url: "http://127.0.0.1:30003"
    api_key: "***"
```

**models** 新增靜態映射表，定義前端顯示的模型名稱：

```yaml
models:
  "hermes-chat": "30000"
  "hermes-coder": "30001"
  "hermes-analyst": "30002"
  "hermes-trader": "30003"
```

### 2. GET /v1/models 端點

- 程式啟動時，從所有 upstream 並行抓取模型列表
- 每個 port 的模型列表獨立快取（TTL 5 分鐘）
- 若 `models` 配置存在，使用靜態名稱；否則使用 upstream 回傳的原始名稱
- 支援 `X-Reload-Port` header 強制重新載入特定 port 的模型

### 3. 路由邏輯

**無 port prefix 的路由** (`/{rest:path}`)：
1. 從請求 body 提取 `model` 欄位
2. 查 `MODEL_ROUTE` 映射表，找到對應的 port
3. 使用該 port 的 `api_key` 作為 Authorization header
4. 轉發到對應的 upstream

**有 port prefix 的路由** (`/{port}/{rest:path}`)：
- 保持原有行為，但改用 `API_KEY_MAP` 中的 key 而非前端傳入的 key

### 4. 模型列表快取策略

- 每個 port 獨立快取
- 當路由到某個 port 時，確保該 port 的模型列表是最新的
- 快取過期或不存在時自動從 upstream 重新抓取

### 5. API Key 傳遞

- 前端傳入的 Authorization header **不再直接使用**
- Tool Filter 根據目標 port，從 `API_KEY_MAP` 取出對應的 key
- 若 port 沒有配置 api_key，則透傳前端的 key

## 好處

1. **Open WebUI 只需一個 API 連線** — 簡化配置
2. **模型名稱乾淨** — 前端看到 "hermes-coder" 而不是 "30001-chatting"
3. **API key 集中管理** — 所有 key 都在 config.yaml，不需要在多個地方重複
4. **動態模型更新** — 上游新增模型時，5 分鐘內自動反映
5. **向後兼容** — 舊的 `/30000/v1/` 路徑仍然可用

## 待討論

1. 是否需要支援 config.yaml 熱重載？（目前需要重啟）
2. 模型名稱衝突時如何處理？（同一個 upstream 有多個模型）
3. 是否需要一個管理端點來手觸發模型列表重新載入？
