# hermes-open-webui-adapter

"Make Hermes tool calls visible in Open WebUI and Conduit app"

An SSE proxy that converts Hermes's custom tool call format into clean Markdown text to avoid rendering issues.

## 功能

- ✅ 多租戶路由（30000/30001/30002/30003）
- ✅ `<details>` 標籤過濾（Conduit APP 相容）
- ✅ Markdown 工具顯示格式
- ✅ 自動會話分割（解決長串流斷線）
- ✅ Emoji 工具映射
- ✅ 健康檢查 API

## 快速開始

```bash
# 安裝依賴
pip install -r requirements.txt

# 啟動
python main.py

# 或透過 systemd
systemctl start hermes-tool-filter
```

## 環境變數

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `AUTO_SPLIT_THRESHOLD` | `4000` | 自動分割閾值（字元數），0 表示關閉 |

## 路由

| 路徑 | 目標 |
|------|------|
| `/30000/v1/*` | `http://127.0.0.1:30000/v1/*` |
| `/30001/v1/*` | `http://127.0.0.1:30001/v1/*` |
| `/30002/v1/*` | `http://127.0.0.1:30002/v1/*` |
| `/30003/v1/*` | `http://127.0.0.1:30003/v1/*` |

## 健康檢查

```bash
curl http://127.0.0.1:9099/health
```

## License

MIT
