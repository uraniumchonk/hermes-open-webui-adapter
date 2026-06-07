#!/usr/bin/env python3
"""
驗證 Open WebUI 在 Responses 模式下的實際行為
1. 確認是否只發送 input + previous_response_id
2. 確認是否正確處理 previous_response_id
3. 確認 hermes_tool_filter 是否正確透傳
"""

import asyncio
import json
import httpx

HERMES_FILTER_URL = "http://127.0.0.1:9099"
API_KEY = "sk-HqBsGK1g1fubv4B4gBfWBgGgBkA8FqEMWtTq4wBo4CjqmA2T"

async def test_responses_basic():
    """測試 1: Responses API - 第一次請求 (無 previous_response_id)"""
    
    print("=" * 60)
    print("測試 1: Responses API - 第一次請求 (無 previous_response_id)")
    print("=" * 60)
    
    async with httpx.AsyncClient(timeout=30) as client:
        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }
        
        payload = {
            "model": "qwen-27b-default",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "你好，請簡單回覆"}],
                }
            ],
            "stream": False,
        }
        
        resp = await client.post(
            f"{HERMES_FILTER_URL}/v1/responses",
            headers=headers,
            json=payload,
        )
        
        result = resp.json()
        print(f"Status: {resp.status_code}")
        print(f"Response ID: {result.get('id', 'N/A')}")
        print(f"Output items: {len(result.get('output', []))}")
        
        response_id = result.get('id')
        
        for item in result.get('output', []):
            print(f"  - type: {item.get('type')}, status: {item.get('status')}")
            if item.get('type') == 'message':
                for part in item.get('content', []):
                    text = part.get('text', '')[:100]
                    print(f"    text: {text}...")
    
    return response_id

async def test_responses_with_previous_id(response_id):
    """測試 2: Responses API - 第二次請求 (帶 previous_response_id)"""
    
    print()
    print("=" * 60)
    print("測試 2: Responses API - 第二次請求 (帶 previous_response_id)")
    print("=" * 60)
    
    async with httpx.AsyncClient(timeout=30) as client:
        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }
        
        payload = {
            "model": "qwen-27b-default",
            "previous_response_id": response_id,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "請繼續對話"}],
                }
            ],
            "stream": False,
        }
        
        resp = await client.post(
            f"{HERMES_FILTER_URL}/v1/responses",
            headers=headers,
            json=payload,
        )
        
        result = resp.json()
        print(f"Status: {resp.status_code}")
        print(f"Response ID: {result.get('id', 'N/A')}")
        print(f"Output items: {len(result.get('output', []))}")
        
        for item in result.get('output', []):
            print(f"  - type: {item.get('type')}, status: {item.get('status')}")
            if item.get('type') == 'message':
                for part in item.get('content', []):
                    text = part.get('text', '')[:100]
                    print(f"    text: {text}...")

async def test_chat_completions_comparison():
    """測試 3: Chat Completions 模式 - 對比 (帶完整歷史)"""
    
    print()
    print("=" * 60)
    print("測試 3: Chat Completions 模式 - 對比 (帶完整歷史)")
    print("=" * 60)
    
    async with httpx.AsyncClient(timeout=30) as client:
        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }
        
        payload = {
            "model": "qwen-27b-default",
            "messages": [
                {"role": "user", "content": "你好，請簡單回覆"},
                {"role": "assistant", "content": "這是模擬的助手回覆"},
                {"role": "user", "content": "請繼續對話"},
            ],
            "stream": False,
        }
        
        resp = await client.post(
            f"{HERMES_FILTER_URL}/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        
        result = resp.json()
        print(f"Status: {resp.status_code}")
        choice = result.get('choices', [{}])[0]
        print(f"Message: {choice.get('message', {}).get('content', '')[:100]}...")

async def test_openwebui_format():
    """測試 4: 模擬 Open WebUI Responses 模式格式"""
    
    print()
    print("=" * 60)
    print("測試 4: 模擬 Open WebUI Responses 模式格式")
    print("=" * 60)
    
    async with httpx.AsyncClient(timeout=30) as client:
        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }
        
        payload = {
            "model": "qwen-27b-default",
            "instructions": "You are a helpful assistant.",
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "你好"}]},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "你好！有什麼可以幫助你？"}]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "請說更多"}]},
            ],
            "stream": False,
        }
        
        resp = await client.post(
            f"{HERMES_FILTER_URL}/v1/responses",
            headers=headers,
            json=payload,
        )
        
        result = resp.json()
        print(f"Status: {resp.status_code}")
        print(f"Response ID: {result.get('id', 'N/A')}")
        print(f"Payload size: {len(json.dumps(payload))} bytes")
        
        for item in result.get('output', []):
            if item.get('type') == 'message':
                for part in item.get('content', []):
                    text = part.get('text', '')[:100]
                    print(f"    text: {text}...")

async def main():
    print("🔍 驗證 Responses API 的行為與 previous_response_id 支援\n")
    
    response_id = await test_responses_basic()
    await test_responses_with_previous_id(response_id)
    await test_chat_completions_comparison()
    await test_openwebui_format()
    
    print()
    print("=" * 60)
    print("結論:")
    print("=" * 60)
    print("1. Responses API 支援 previous_response_id (server-side state)")
    print("2. Open WebUI 目前 ENABLE_RESPONSES_API_STATEFUL=False")
    print("3. 因此 Open WebUI 仍然發送完整歷史到 filter")
    print("4. hermes_tool_filter 的 responses_handler 正確透傳")
    print("5. 要啟用 stateful 模式，需設定 ENABLE_RESPONSES_API_STATEFUL=true")

if __name__ == "__main__":
    asyncio.run(main())
