#!/usr/bin/env python3
"""
測試 Responses API 的 Tool Calling - 直接呼叫 gateway + proxy
"""
import aiohttp
import asyncio
import json

PROXY_BASE = "http://127.0.0.1:9099/30000"
PROXY_KEY = "hermes_chat_key"

GATEWAY_BASE = "http://127.0.0.1:30000"
GATEWAY_KEY = "hermes_chat_key"

async def test_tool_call_direct():
    """測試 1: 直接呼叫 Gateway - 帶 tools 的 Responses 請求"""
    print("=" * 70)
    print("🧪 測試 1: 直接呼叫 Gateway - Responses API + Tool Calling")
    print("=" * 70)
    
    async with aiohttp.ClientSession() as sess:
        payload = {
            "model": "hermes-agent",
            "input": "搜尋一下今天的BTC價格",
            "stream": False,
            "tools": [
                {
                    "type": "function",
                    "name": "web_search",
                    "description": "搜尋網路",
                    "strict": False,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "搜尋關鍵字"}
                        },
                        "required": ["query"]
                    }
                }
            ],
        }
        
        try:
            async with sess.post(
                f"{GATEWAY_BASE}/v1/responses", json=payload,
                headers={"Authorization": f"Bearer {GATEWAY_KEY}"},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                print(f"Status: {resp.status}")
                data = await resp.json()
                if 'error' in data:
                    print(f"❌ Error: {data['error']}")
                    return False
                
                output = data.get('output', [])
                print(f"Output items: {len(output)}")
                
                for item in output:
                    item_type = item.get('type', 'unknown')
                    if item_type == 'message':
                        for part in item.get('content', []):
                            if part.get('type') == 'output_text':
                                print(f"  Text: {part['text'][:200]}")
                    elif item_type == 'function_call':
                        print(f"  ✅ Function call: {item.get('name')}")
                        print(f"     Args: {item.get('arguments', '')[:200]}")
                
                print("✅ 測試通過！")
                return True
        except Exception as e:
            print(f"❌ 測試失敗: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return False

async def test_tool_call_proxy():
    """測試 2: 經過 Proxy - 帶 tools 的 Responses 請求"""
    print("\n" + "=" * 70)
    print("🧪 測試 2: 經過 Proxy - Responses API + Tool Calling")
    print("=" * 70)
    
    async with aiohttp.ClientSession() as sess:
        payload = {
            "model": "hermes-agent",
            "input": "搜尋一下今天的BTC價格",
            "stream": False,
            "tools": [
                {
                    "type": "function",
                    "name": "web_search",
                    "description": "搜尋網路",
                    "strict": False,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "搜尋關鍵字"}
                        },
                        "required": ["query"]
                    }
                }
            ],
        }
        
        try:
            async with sess.post(
                f"{PROXY_BASE}/v1/responses", json=payload,
                headers={"Authorization": f"Bearer {PROXY_KEY}"},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                print(f"Status: {resp.status}")
                data = await resp.json()
                if 'error' in data:
                    print(f"❌ Error: {data['error']}")
                    return False
                
                output = data.get('output', [])
                print(f"Output items: {len(output)}")
                
                for item in output:
                    item_type = item.get('type', 'unknown')
                    if item_type == 'message':
                        for part in item.get('content', []):
                            if part.get('type') == 'output_text':
                                print(f"  Text: {part['text'][:200]}")
                    elif item_type == 'function_call':
                        print(f"  ✅ Function call: {item.get('name')}")
                        print(f"     Args: {item.get('arguments', '')[:200]}")
                
                print("✅ 測試通過！")
                return True
        except Exception as e:
            print(f"❌ 測試失敗: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return False

async def test_chat_with_tools_proxy():
    """測試 3: Chat Completions 帶 tools 經過 proxy"""
    print("\n" + "=" * 70)
    print("🧪 測試 3: Chat Completions + Tools 經過 Proxy")
    print("=" * 70)
    
    async with aiohttp.ClientSession() as sess:
        payload = {
            "model": "hermes-agent",
            "messages": [{"role": "user", "content": "搜尋一下今天的BTC價格"}],
            "stream": False,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "description": "搜尋網路",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"}
                            },
                            "required": ["query"]
                        }
                    }
                }
            ],
        }
        
        try:
            async with sess.post(
                f"{PROXY_BASE}/v1/chat/completions", json=payload,
                headers={"Authorization": f"Bearer {PROXY_KEY}"},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                print(f"Status: {resp.status}")
                data = await resp.json()
                if 'error' in data:
                    print(f"❌ Error: {data['error']}")
                    return False
                
                choices = data.get('choices', [])
                if choices:
                    msg = choices[0].get('message', {})
                    content = msg.get('content', '')
                    tool_calls = msg.get('tool_calls', [])
                    print(f"Content: {content[:200]}")
                    print(f"Tool calls: {len(tool_calls)}")
                    for tc in tool_calls:
                        fn = tc.get('function', {})
                        print(f"  - {fn.get('name', 'unknown')}: {fn.get('arguments', '')[:100]}")
                
                print("✅ 測試通過！")
                return True
        except Exception as e:
            print(f"❌ 測試失敗: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return False

async def main():
    r1 = await test_tool_call_direct()
    r2 = await test_tool_call_proxy()
    r3 = await test_chat_with_tools_proxy()
    
    print("\n" + "=" * 70)
    print("📊 總結:")
    print(f"  直接 Tool Call: {'✅' if r1 else '❌'}")
    print(f"  Proxy Tool Call (Responses): {'✅' if r2 else '❌'}")
    print(f"  Proxy Tool Call (Chat): {'✅' if r3 else '❌'}")
    print("=" * 70)

asyncio.run(main())
