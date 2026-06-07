#!/usr/bin/env python3
"""
全面測試 Responses API 行為
測試與除錯 Responses API 的實際行為
"""

import asyncio
import json
import httpx

API_KEY="hermes_chat_key"
GATEWAY = "http://127.0.0.1:30000"
FILTER = "http://127.0.0.1:9099"
OPENWEBUI = "http://127.0.0.1:30010"

async def test_raw_gateway():
    """測試 1: 直接對原始 Gateway 發送 Responses API"""
    
    print("=" * 60)
    print("測試 1: 原始 Gateway - Responses 非串流")
    print("=" * 60)
    
    async with httpx.AsyncClient(timeout=30) as client:
        payload = {
            "model": "qwen-27b-default",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "你好，請簡單回覆 \"這是測試\""}],
                }
            ],
            "stream": False,
        }
        
        resp = await client.post(
            f"{GATEWAY}/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json=payload,
        )
        
        result = resp.json()
        print(f"Status: {resp.status_code}")
        print(f"Response Keys: {list(result.keys())}")
        print(f"Response ID: {result.get('id', 'N/A')}")
        print(f"Output count: {len(result.get('output', []))}")
        for item in result.get('output', []):
            print(f"  Output type: {item.get('type')}")
            if item.get('type') == 'message':
                for part in item.get('content', []):
                    print(f"    Content type: {part.get('type')}, text: {part.get('text', '')[:80]}...")
        
        return result.get('id')

async def test_responses_with_previous(prev_id):
    """測試 2: 帶 previous_response_id"""
    
    print()
    print("=" * 60)
    print("測試 2: Responses API - 帶 previous_response_id")
    print("=" * 60)
    
    async with httpx.AsyncClient(timeout=30) as client:
        payload = {
            "model": "qwen-27b-default",
            "previous_response_id": prev_id,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "繼續對話，說更多"}],
                }
            ],
            "stream": False,
        }
        
        resp = await client.post(
            f"{GATEWAY}/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json=payload,
        )
        
        result = resp.json()
        print(f"Status: {resp.status_code}")
        print(f"Response ID: {result.get('id', 'N/A')}")
        
        if resp.status_code == 200:
            for item in result.get('output', []):
                if item.get('type') == 'message':
                    for part in item.get('content', []):
                        print(f"    text: {part.get('text', '')[:80]}...")
        else:
            print(f"Error: {result}")

async def test_via_filter():
    """測試 3: 透過 hermes_tool_filter 發送 Responses API"""
    
    print()
    print("=" * 60)
    print("測試 3: 透過 Filter - Responses 非串流 (/30000/v1/responses)")
    print("=" * 60)
    
    async with httpx.AsyncClient(timeout=60) as client:
        payload = {
            "model": "qwen-27b-default",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "你好！請簡單回應 \"FILTER測試\""}],
                }
            ],
            "stream": False,
        }
        
        resp = await client.post(
            f"{FILTER}/30000/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json=payload,
        )
        
        print(f"Status: {resp.status_code}")
        print(f"Headers: content-type={resp.headers.get('content-type')}")
        
        try:
            result = resp.json()
            print(f"Response ID: {result.get('id', 'N/A')}")
            print(f"Output count: {len(result.get('output', []))}")
            for item in result.get('output', []):
                print(f"  type: {item.get('type')}")
                if item.get('type') == 'message':
                    for part in item.get('content', []):
                        print(f"    text: {part.get('text', '')[:80]}...")
        except:
            print(f"Raw response (first 500 chars): {resp.text[:500]}")

async def test_filter_streaming():
    """測試 4: 透過 Filter 的 Responses 串流"""
    
    print()
    print("=" * 60)
    print("測試 4: 透過 Filter - Responses 串流")
    print("=" * 60)
    
    async with httpx.AsyncClient(timeout=60) as client:
        payload = {
            "model": "qwen-27b-default",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "說一句你好"}],
                }
            ],
            "stream": True,
        }
        
        async with client.stream(
            "POST",
            f"{FILTER}/30000/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json=payload,
        ) as resp:
            print(f"Status: {resp.status_code}")
            print(f"Content-Type: {resp.headers.get('content-type')}")
            
            event_count = 0
            async for line in resp.aiter_lines():
                if line:
                    print(f"  SSE: {line[:200]}")
                    event_count += 1
                    if event_count >= 10:
                        print(f"  ... (more events)")
                        break

async def test_chat_completions():
    """測試 5: 透過 Filter 的 Chat Completions（對照組）"""
    
    print()
    print("=" * 60)
    print("測試 5: 透過 Filter - Chat Completions 非串流")
    print("=" * 60)
    
    async with httpx.AsyncClient(timeout=30) as client:
        payload = {
            "model": "qwen-27b-default",
            "messages": [
                {"role": "system", "content": "你是測試助手"},
                {"role": "user", "content": "請簡單回答「測試成功」"}
            ],
            "stream": False,
        }
        
        resp = await client.post(
            f"{FILTER}/30000/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json=payload,
        )
        
        print(f"Status: {resp.status_code}")
        try:
            result = resp.json()
            choice = result.get('choices', [{}])[0]
            msg = choice.get('message', {})
            print(f"Role: {msg.get('role')}")
            print(f"Content: {msg.get('content', '')[:100]}...")
        except:
            print(f"Raw: {resp.text[:300]}")

async def test_openwebui_models():
    """測試 6: Open WebUI 的 models 端點"""
    
    print()
    print("=" * 60)
    print("測試 6: Open WebUI - 列出 models")
    print("=" * 60)
    
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{OPENWEBUI}/api/v1/models",
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        print(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            if 'data' in data:
                for m in data['data'][:3]:
                    print(f"  Model: {m.get('id')}")
            else:
                print(f"Keys: {list(data.keys())}")
        else:
            print(f"Error: {resp.text[:200]}")

async def main():
    print("🔍 全面測試 Responses API 行為\n")
    
    # 測試 1: 原始 Gateway
    rid = await test_raw_gateway()
    
    # 測試 2: previous_response_id
    if rid:
        await test_responses_with_previous(rid)
    
    # 測試 3: 透過 Filter
    await test_via_filter()
    
    # 測試 4: Filter 串流
    await test_filter_streaming()
    
    # 測試 5: Chat Completions 對照
    await test_chat_completions()
    
    # 測試 6: Open WebUI
    await test_openwebui_models()
    
    print("\n" + "=" * 60)
    print("✅ 測試完成")

if __name__ == "__main__":
    asyncio.run(main())
