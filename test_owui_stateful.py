#!/usr/bin/env python3
"""
完整測試：Open WebUI Responses Stateful 模式
驗證 Open WebUI 在 ENABLE_RESPONSES_API_STATEFUL=true 下是否正確只發送新訊息
"""

import asyncio
import httpx
import json
import time

# 端點設定
GATEWAY_URL = "http://127.0.0.1:30000/v1"
PROXY_URL = "http://127.0.0.1:9099/30000/v1"
API_KEY = "hermes_chat_key"
MODEL = "qwen-27b-default"

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

async def test_stateful_responses_via_proxy():
    """
    透過 hermes_tool_filter (port 9099) 測試 Responses API 的 Stateful 模式
    模擬 Open WebUI 的行為：只發送 input + previous_response_id
    """
    print("=" * 60)
    print("🧪 測試 1: Stateful Responses API (透過 Proxy)")
    print("=" * 60)
    
    async with httpx.AsyncClient(timeout=60) as client:
        # 第一輪：告訴模型記住一個數字
        print("\n📝 第一輪：告訴模型記住數字 9527")
        resp1 = await client.post(
            f"{PROXY_URL}/responses",
            headers=HEADERS,
            json={
                "model": MODEL,
                "input": [{"type": "message", "role": "user", "content": "請記住數字 9527，之後我會問你。"}],
                "stream": False,
            }
        )
        
        if resp1.status_code != 200:
            print(f"❌ 第一輪失敗: {resp1.status_code} - {resp1.text[:200]}")
            return
        
        data1 = resp1.json()
        response_id_1 = data1.get("id", "unknown")
        output1 = data1.get("output", [])
        
        assistant_text_1 = ""
        for item in output1:
            if item.get("type") == "message" and item.get("content"):
                for c in item["content"]:
                    if c.get("type") in ("text", "output_text"):
                        assistant_text_1 += c.get("text", "")
        
        print(f"✅ 第一輪成功")
        print(f"   Response ID: {response_id_1}")
        print(f"   原始 output: {json.dumps(output1, ensure_ascii=False)[:500]}")
        print(f"   模型回覆: {assistant_text_1[:100]}...")
        
        # 第二輪：只傳 previous_response_id + 新 input（不帶歷史！）
        print(f"\n📝 第二輪：只傳 previous_response_id + 新問題（不帶歷史）")
        resp2 = await client.post(
            f"{PROXY_URL}/responses",
            headers=HEADERS,
            json={
                "model": MODEL,
                "input": [{"type": "message", "role": "user", "content": "我剛才讓你記住的數字是多少？"}],
                "previous_response_id": response_id_1,
                "stream": False,
            }
        )
        
        if resp2.status_code != 200:
            print(f"❌ 第二輪失敗: {resp2.status_code} - {resp2.text[:200]}")
            return
        
        data2 = resp2.json()
        output2 = data2.get("output", [])
        
        assistant_text_2 = ""
        for item in output2:
            if item.get("type") == "message" and item.get("content"):
                for c in item["content"]:
                    if c.get("type") in ("text", "output_text"):
                        assistant_text_2 += c.get("text", "")
        
        print(f"✅ 第二輪成功")
        print(f"   原始 output: {json.dumps(output2, ensure_ascii=False)[:500]}")
        print(f"   模型回覆: {assistant_text_2[:200]}...")
        
        # 驗證模型是否記得
        if "9527" in assistant_text_2 or "9527" in assistant_text_2.lower():
            print(f"\n✅✅✅ 模型正確記住了數字 9527！Stateful 模式運作正常！")
        else:
            print(f"\n❌ 模型沒有記住數字 9527")
            print(f"   回覆: {assistant_text_2}")
        
        return data2

async def test_chat_completions_still_works():
    """
    確認 Chat Completions 模式仍然正常運作（不受 Responses 影響）
    """
    print("\n" + "=" * 60)
    print("🧪 測試 2: Chat Completions 對照測試")
    print("=" * 60)
    
    async with httpx.AsyncClient(timeout=60) as client:
        print("\n📝 發送 Chat Completions 請求")
        resp = await client.post(
            f"{PROXY_URL}/chat/completions",
            headers=HEADERS,
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "用一句話回答：1+1等於幾？"}],
                "stream": False,
            }
        )
        
        if resp.status_code != 200:
            print(f"❌ Chat Completions 失敗: {resp.status_code}")
            return
        
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        print(f"✅ Chat Completions 正常")
        print(f"   回覆: {content[:100]}...")

async def test_streaming_via_proxy():
    """
    驗證 Responses API 串流模式透過 Proxy 正常運作
    """
    print("\n" + "=" * 60)
    print("🧪 測試 3: Responses Streaming 模式")
    print("=" * 60)
    
    async with httpx.AsyncClient(timeout=60) as client:
        print("\n📝 發送串流 Responses 請求")
        async with client.stream(
            "POST",
            f"{PROXY_URL}/responses",
            headers={**HEADERS, "Accept": "text/event-stream"},
            json={
                "model": MODEL,
                "input": [{"type": "message", "role": "user", "content": "用三個字回答：貓會叫什麼？"}],
                "stream": True,
            }
        ) as resp:
            if resp.status_code != 200:
                print(f"❌ 串流失敗: {resp.status_code}")
                return
            
            events = []
            full_text = ""
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(line[6:])
                if line.startswith("data: {" ) and "output_text.delta" in line:
                    try:
                        evt = json.loads(line[6:])
                        if "output_text" in evt and "delta" in evt["output_text"]:
                            full_text += evt["output_text"]["delta"]
                    except:
                        pass
            
            print(f"✅ 串流成功，共 {len(events)} 個事件")
            print(f"   完整文字: {full_text[:100]}...")

async def test_openwebui_connections_config():
    """
    直接從 Open WebUI 的 API 檢查連接設定
    """
    print("\n" + "=" * 60)
    print("🧪 測試 4: Open WebUI 連接設定檢查")
    print("=" * 60)
    
    async with httpx.AsyncClient(timeout=10) as client:
        # 先取得 token
        login_resp = await client.post(
            "http://127.0.0.1:30010/api/v1/auth/login",
            json={"identity": "hermes@meowmeow.uk", "password": "Meow2018!@#"},
        )
        
        if login_resp.status_code != 200:
            print(f"❌ 登入失敗: {login_resp.status_code} - {login_resp.text[:200]}")
            return
        
        token = login_resp.json().get("data", {}).get("token", "")
        if not token:
            print("❌ 沒有拿到 token")
            return
        
        auth_headers = {"Authorization": f"Bearer {token}"}
        
        # 取得 connections 設定
        conn_resp = await client.get(
            "http://127.0.0.1:30010/api/v1/admin/settings",
            headers=auth_headers,
        )
        
        if conn_resp.status_code != 200:
            print(f"❌ 取得設定失敗: {conn_resp.status_code}")
            return
        
        settings = conn_resp.json().get("data", {})
        oi = settings.get("oi", {})
        connections = oi.get("connections", [])
        
        print(f"\n📊 共 {len(connections)} 個連接:")
        for i, conn in enumerate(connections):
            name = conn.get("name", "N/A")
            api_type = conn.get("api_type", "N/A")
            url = conn.get("url", conn.get("base_url", "N/A"))
            print(f"   {i+1}. {name} → api_type={api_type}, url={url}")
        
        # 檢查是否有 responses 模式
        responses_conns = [c for c in connections if c.get("api_type") == "responses"]
        if responses_conns:
            print(f"\n✅ 有 {len(responses_conns)} 個連接使用 Responses 模式")
        else:
            print(f"\n⚠️  目前所有連接都是 chat_completions 模式")

async def main():
    print("🚀 Open WebUI Responses Stateful 模式完整測試")
    print("=" * 60)
    
    # 測試 1: Stateful Responses
    await test_stateful_responses_via_proxy()
    
    # 測試 2: Chat Completions 對照
    await test_chat_completions_still_works()
    
    # 測試 3: Streaming
    await test_streaming_via_proxy()
    
    # 測試 4: Open WebUI 設定檢查
    await test_openwebui_connections_config()
    
    print("\n" + "=" * 60)
    print("🎉 全部測試完成！")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
