#!/usr/bin/env python3
"""
測試 Responses API 的 Tool Calling 行為 - 透過 Open WebUI (30010)
"""
import aiohttp
import asyncio
import json

# 直接透過 Open WebUI 前端測試
OWUI_BASE = "http://127.0.0.1:30010"
OWUI_EMAIL = "87000861+uraniumchonk@users.noreply.github.com"
OWUI_PASSWORD = "Th19731112"

async def get_owui_session():
    """取得 Open WebUI 的 session cookie"""
    print("=" * 70)
    print("🔑 取得 Open WebUI Session")
    print("=" * 70)
    
    async with aiohttp.ClientSession() as sess:
        try:
            async with sess.post(
                f"{OWUI_BASE}/api/v1/auths",
                json={"email": OWUI_EMAIL, "password": OWUI_PASSWORD},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                if resp.status == 200:
                    token = data.get("token", "")
                    print(f"✅ Login successful, token: {token[:20]}...")
                    return token
                else:
                    print(f"❌ Login failed: {data}")
                    return None
        except Exception as e:
            print(f"❌ Error: {e}")
            return None

async def test_chat_with_tools(token):
    """測試 1: 透過 Open WebUI 發送帶有工具呼叫的請求"""
    print("\n" + "=" * 70)
    print("🧪 測試: 透過 Open WebUI 發送 Chat Completions (帶工具)")
    print("=" * 70)
    
    if not token:
        print("❌ No token")
        return False
    
    async with aiohttp.ClientSession() as sess:
        headers = {"Authorization": f"Bearer {token}"}
        
        # 先取得可用的 models
        try:
            async with sess.get(f"{OWUI_BASE}/api/v1/models", headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                models = await resp.json()
                print(f"Models: {len(models)} available")
                for m in models[:3]:
                    print(f"  - {m.get('name', 'unknown')}")
        except Exception as e:
            print(f"❌ Model list error: {e}")
        
        # 發送聊天請求
        payload = {
            "messages": [{"role": "user", "content": "搜尋一下今天的BTC價格"}],
            "stream": False,
        }
        
        try:
            async with sess.post(
                f"{OWUI_BASE}/api/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
                if 'error' in data:
                    print(f"❌ Error: {data['error']}")
                    return False
                choices = data.get('choices', [])
                if choices:
                    msg = choices[0].get('message', {})
                    content = msg.get('content', '')
                    tool_calls = msg.get('tool_calls', [])
                    print(f"Response content: {content[:200]}")
                    print(f"Tool calls: {len(tool_calls)}")
                    for tc in tool_calls:
                        print(f"  - {tc.get('function', {}).get('name', 'unknown')}")
                return True
        except Exception as e:
            print(f"❌ 測試失敗: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return False

async def main():
    token = await get_owui_session()
    await test_chat_with_tools(token)

asyncio.run(main())
