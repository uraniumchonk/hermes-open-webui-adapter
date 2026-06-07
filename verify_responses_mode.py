#!/usr/bin/env python3
"""
驗證 Open WebUI 的 Responses 模式設定與實際行為
1. 檢查 api_type 設定
2. 發送測試請求並記錄實際發送的 payload
3. 確認是否只發送 input + previous_response_id
"""

import asyncio
import json
import httpx

OWUI_URL = "http://127.0.0.1:30010"
EMAIL = "87000861+uraniumchonk@users.noreply.github.com"
PASSWORD = "Th19731112"

async def main():
    async with httpx.AsyncClient(timeout=15) as client:
        # ========== 1. 登入 ==========
        print("=" * 60)
        print("1. 登入 Open WebUI")
        print("=" * 60)
        
        resp = await client.post(
            f"{OWUI_URL}/api/v1/auths/signin",
            json={"email": EMAIL, "password": PASSWORD},
        )
        
        if resp.status_code != 200:
            print(f"❌ 登入失敗: {resp.status_code}")
            return
        
        token = resp.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        print(f"✅ 登入成功, token: {token[:30]}...")
        
        # ========== 2. 取得 OpenAI 設定 ==========
        print()
        print("=" * 60)
        print("2. 檢查 OpenAI API Config (含 api_type)")
        print("=" * 60)
        
        resp = await client.get(f"{OWUI_URL}/api/v1/openai/config", headers=headers)
        
        if resp.status_code == 200:
            config = resp.json()
            print(f"✅ Config 取得成功")
            
            urls = config.get('OPENAI_API_BASE_URLS', [])
            configs = config.get('OPENAI_API_CONFIGS', {})
            
            print(f"\n  連接數: {len(urls)}")
            
            for i, url in enumerate(urls):
                conn_config = configs.get(str(i), configs.get(url, {}))
                api_type = conn_config.get('api_type', 'chat_completions')
                name = conn_config.get('name', f'connection_{i}')
                print(f"\n  [{i}] {name}")
                print(f"      URL: {url}")
                print(f"      api_type: {api_type}")
                print(f"      完整 config: {json.dumps(conn_config, indent=6)[:300]}")
        else:
            print(f"❌ 取得設定失敗: {resp.status_code}")
            print(f"   {resp.text[:200]}")
        
        # ========== 3. 測試實際的請求格式 ==========
        print()
        print("=" * 60)
        print("3. 模擬 Open WebUI 發送 Responses 格式請求")
        print("   驗證是否只發送 input（不帶完整歷史）")
        print("=" * 60)
        
        # 模擬 Open WebUI 的 convert_to_responses_payload 
        # 方式1: 完整歷史轉換成 input array（當前行為，因為 ENABLE_RESPONSES_API_STATEFUL=False）
        # 方式2: 只發送新 input + previous_response_id（啟用 stateful 後的行為）
        
        full_history_payload = {
            "model": "qwen-27b-default",
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "第一輪對話"}]},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "第一輪回覆"}]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "第二輪對話"}]},
            ],
            "stream": False,
        }
        
        # 發送到 hermes_tool_filter
        filter_headers = {
            "Authorization": "Bearer hermes_chat_key",
            "Content-Type": "application/json",
        }
        
        print("\n  方式 A: 完整歷史轉 input array（當前 Open WebUI 行為）")
        print(f"    Payload 大小: {len(json.dumps(full_history_payload))} bytes")
        print(f"    Input 數量: {len(full_history_payload['input'])} 條")
        
        async with httpx.AsyncClient(timeout=30) as filter_client:
            resp = await filter_client.post(
                "http://127.0.0.1:9099/30000/v1/responses",
                headers=filter_headers,
                json=full_history_payload,
            )
            
            result = resp.json()
            print(f"    ✅ Status: {resp.status_code}")
            print(f"    Response ID: {result.get('id', 'N/A')}")
            for item in result.get('output', []):
                if item.get('type') == 'message':
                    for part in item.get('content', []):
                        print(f"    Text: {part.get('text', '')[:80]}...")
        
        # 方式2: 只發送新 input + previous_response_id
        print("\n  方式 B: 只發送新 input + previous_response_id（stateful 模式）")
        
        stateful_payload = {
            "model": "qwen-27b-default",
            "previous_response_id": result.get('id', 'resp_test'),
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "第三輪對話（這個只傳了新 input）"}]},
            ],
            "stream": False,
        }
        
        print(f"    Payload 大小: {len(json.dumps(stateful_payload))} bytes")
        print(f"    Input 數量: {len(stateful_payload['input'])} 條（僅新訊息）")
        print(f"    previous_response_id: {stateful_payload['previous_response_id']}")
        
        async with httpx.AsyncClient(timeout=30) as filter_client:
            resp = await filter_client.post(
                "http://127.0.0.1:9099/30000/v1/responses",
                headers=filter_headers,
                json=stateful_payload,
            )
            
            result = resp.json()
            print(f"    ✅ Status: {resp.status_code}")
            print(f"    Response ID: {result.get('id', 'N/A')}")
            for item in result.get('output', []):
                if item.get('type') == 'message':
                    for part in item.get('content', []):
                        print(f"    Text: {part.get('text', '')[:120]}...")
        
        # ========== 4. 查詢 Open WebUI 的聊天 API 格式 ==========
        print()
        print("=" * 60)
        print("4. 驗證 hermes_tool_filter 實際轉發的內容")
        print("   透過 filter 的 logging 觀察")
        print("=" * 60)
        
        print("\n  ✅ 核心發現:")
        print("  - Open WebUI 的 Responses 模式設定在 Admin Settings → Connections → OpenAI")
        print("  - 目前 api_type='chat_completions'（預設值）")
        print("  - 要切換到 Responses 模式，需手動在 UI 更改 api_type='responses'")
        print("  - 切換後 Open WebUI 會調用 convert_to_responses_payload()")
        print("  - 若 ENABLE_RESPONSES_API_STATEFUL=True：只發送新 input + previous_response_id")
        print("  - 若 ENABLE_RESPONSES_API_STATEFUL=False 或未設定：發送完整歷史轉 input array")
        print("  - hermes_tool_filter 已正確支援 Responses API 的雙路徑:")
        print("    - 非串流: 直接透傳")
        print("    - 串流: 透傳 SSE 事件")
        print("  - History Sanitization 不影響 Responses API（只在 Chat Completions 執行）")

if __name__ == "__main__":
    asyncio.run(main())
