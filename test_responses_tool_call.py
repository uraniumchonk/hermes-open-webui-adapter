#!/usr/bin/env python3
"""
測試：Responses API Stateful 模式下的工具調用記憶
驗證模型是否能記住之前工具調用的結果
"""

import asyncio
import httpx
import json

PROXY_URL = "http://127.0.0.1:9099/30000/v1"
API_KEY = "hermes_chat_key"
MODEL = "qwen-27b-default"

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

async def test_tool_call_memory():
    """
    測試：在第一輪使用工具，第二輪詢問工具結果
    """
    print("=" * 60)
    print("🧪 測試: 工具調用上下文記憶 (Stateful 模式)")
    print("=" * 60)
    
    async with httpx.AsyncClient(timeout=120) as client:
        # 第一輪：讓模型使用工具
        print("\n📝 第一輪：讓模型查詢倉位")
        resp1 = await client.post(
            f"{PROXY_URL}/responses",
            headers=HEADERS,
            json={
                "model": MODEL,
                "input": [{"type": "message", "role": "user", "content": "請查詢我的倉位"}],
                "stream": False,
            }
        )
        
        if resp1.status_code != 200:
            print(f"❌ 第一輪失敗: {resp1.status_code}")
            return
        
        data1 = resp1.json()
        response_id_1 = data1.get("id", "unknown")
        output1 = data1.get("output", [])
        
        print(f"\n📊 第一輪 Response ID: {response_id_1}")
        print(f"📊 Output items ({len(output1)} 個):")
        for i, item in enumerate(output1):
            item_type = item.get("type", "unknown")
            if item_type == "message":
                for c in item.get("content", []):
                    if c.get("type") in ("text", "output_text"):
                        print(f"   [{i}] message: {c.get('text', '')[:100]}")
            elif item_type == "function_call":
                print(f"   [{i}] function_call: {item.get('name')} → {item.get('arguments', '')[:100]}")
            elif item_type == "function_call_output":
                print(f"   [{i}] function_call_output: {item.get('output', '')[:100]}")
            else:
                print(f"   [{i}] {item_type}")
        
        # 第二輪：只傳 previous_response_id + 新問題
        print(f"\n📝 第二輪：只傳 previous_response_id + 新問題")
        resp2 = await client.post(
            f"{PROXY_URL}/responses",
            headers=HEADERS,
            json={
                "model": MODEL,
                "input": [{"type": "message", "role": "user", "content": "我剛才查詢的倉位中，哪個虧損最多？"}],
                "previous_response_id": response_id_1,
                "stream": False,
            }
        )
        
        if resp2.status_code != 200:
            print(f"❌ 第二輪失敗: {resp2.status_code}")
            return
        
        data2 = resp2.json()
        output2 = data2.get("output", [])
        
        assistant_text_2 = ""
        for item in output2:
            if item.get("type") == "message" and item.get("content"):
                for c in item["content"]:
                    if c.get("type") in ("text", "output_text"):
                        assistant_text_2 += c.get("text", "")
        
        print(f"\n📊 第二輪回覆:")
        print(f"   {assistant_text_2[:500]}")
        
        # 檢查模型是否記得工具結果
        if "虧損" in assistant_text_2 or "loss" in assistant_text_2.lower() or "最多" in assistant_text_2:
            print(f"\n✅ 模型記得工具結果！")
        else:
            print(f"\n❌ 模型不記得工具結果！")
            print(f"   回覆沒有提到虧損最多的倉位")
        
        return data2

async def test_tool_call_via_chat_completions():
    """
    對照測試：Chat Completions 模式下的工具調用記憶
    """
    print("\n" + "=" * 60)
    print("🧪 對照: Chat Completions 模式下的工具調用")
    print("=" * 60)
    
    async with httpx.AsyncClient(timeout=120) as client:
        # 第一輪
        print("\n📝 第一輪：讓模型查詢倉位")
        resp1 = await client.post(
            f"{PROXY_URL}/chat/completions",
            headers=HEADERS,
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "請查詢我的倉位"}],
                "stream": False,
            }
        )
        
        if resp1.status_code != 200:
            print(f"❌ 第一輪失敗: {resp1.status_code}")
            return
        
        data1 = resp1.json()
        assistant_msg_1 = data1["choices"][0]["message"]
        
        print(f"\n📊 第一輪回覆:")
        print(f"   {assistant_msg_1.get('content', '')[:200]}")
        print(f"   工具調用: {len(assistant_msg_1.get('tool_calls', []))} 個")
        
        # 組建完整歷史（包含工具訊息）
        history = [
            {"role": "user", "content": "請查詢我的倉位"},
            assistant_msg_1,
        ]
        
        # 如果有工具調用，加入工具結果
        if assistant_msg_1.get("tool_calls"):
            for tc in assistant_msg_1["tool_calls"]:
                # 模擬工具結果
                history.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": "倉位資料: BTC -0.5%, ETH +1.2%, SOL -2.3%",
                })
        
        # 第二輪
        print(f"\n📝 第二輪：完整歷史 + 新問題")
        resp2 = await client.post(
            f"{PROXY_URL}/chat/completions",
            headers=HEADERS,
            json={
                "model": MODEL,
                "messages": history + [{"role": "user", "content": "我剛才查詢的倉位中，哪個虧損最多？"}],
                "stream": False,
            }
        )
        
        if resp2.status_code != 200:
            print(f"❌ 第二輪失敗: {resp2.status_code}")
            return
        
        data2 = resp2.json()
        content = data2["choices"][0]["message"]["content"]
        
        print(f"\n📊 第二輪回覆:")
        print(f"   {content[:300]}")

async def main():
    print("🚀 工具調用上下文記憶測試")
    print("=" * 60)
    
    await test_tool_call_memory()
    await test_tool_call_via_chat_completions()
    
    print("\n" + "=" * 60)
    print("🎉 測試完成！")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
