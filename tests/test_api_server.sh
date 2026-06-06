#!/bin/bash
# 測試修改後的 API Server SSE 輸出
curl -s -N http://127.0.0.1:30000/v1/chat/completions \
  -H "Authorization: Bearer *** \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen-27b-default",
    "messages": [
      {"role": "user", "content": "用 web_search 搜尋台中明天天氣"}
    ],
    "stream": true,
    "max_tokens": 300
  }' 2>&1 | timeout 60 grep -A1 "hermes.tool.progress"
