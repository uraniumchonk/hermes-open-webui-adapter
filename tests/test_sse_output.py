#!/usr/bin/env python3
"""
測試 hermes_tool_filter 的 SSE 輸出，檢查 <details> 標籤是否正確傳遞。
"""
import asyncio
import aiohttp
import json

async def test_sse_output():
    url = "http://127.0.0.1:9099/30000/v1/chat/completions"
    headers = {
        "Authorization": "Bearer ***",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "qwen-27b-default",
        "messages": [
            {"role": "user", "content": "用 web_search 搜尋台中明天天氣"}
        ],
        "stream": True,
        "max_tokens": 300,
    }
    
    print("=" * 80)
    print("開始測試 SSE 輸出...")
    print("=" * 80)
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            print(f"\nHTTP Status: {resp.status}\n")
            
            chunk_count = 0
            details_found = False
            tool_calls_found = False
            tool_role_found = False
            
            async for line in resp.content:
                line_str = line.decode('utf-8', errors='replace').strip()
                
                if not line_str or line_str.startswith(':'):
                    continue
                
                if line_str.startswith('data:'):
                    data = line_str[5:].strip()
                    chunk_count += 1
                    
                    if data == '[DONE]':
                        print(f"\n[{chunk_count}] [DONE]")
                        break
                    
                    try:
                        parsed = json.loads(data)
                        choice = parsed.get('choices', [{}])[0]
                        delta = choice.get('delta', {})
                        finish_reason = choice.get('finish_reason')
                        
                        if 'role' in delta:
                            print(f"\n[{chunk_count}] role: {delta['role']}")
                            if delta['role'] == 'tool':
                                tool_role_found = True
                                print(f"  ⚠️ 發現 role:tool !")
                        
                        if 'tool_calls' in delta:
                            tool_calls_found = True
                            print(f"\n[{chunk_count}] tool_calls: {json.dumps(delta['tool_calls'], ensure_ascii=False)}")
                        
                        if 'content' in delta:
                            content = delta['content']
                            if '<details' in content:
                                details_found = True
                                print(f"\n[{chunk_count}] <details> 標籤:")
                                print(f"  {content[:200]}...")
                            elif len(content) < 100:
                                print(f"[{chunk_count}] content: {content}")
                        
                        if finish_reason:
                            print(f"\n[{chunk_count}] finish_reason: {finish_reason}")
                            usage = parsed.get('usage', {})
                            if usage:
                                print(f"  usage: {usage}")
                    
                    except json.JSONDecodeError:
                        pass
    
    print("\n" + "=" * 80)
    print("測試結果總結:")
    print(f"  總 chunk 數: {chunk_count}")
    print(f"  <details> 標籤: {'✅ 找到' if details_found else '❌ 未找到'}")
    print(f"  tool_calls delta: {'✅ 找到' if tool_calls_found else '❌ 未找到'}")
    print(f"  role:tool: {'✅ 找到' if tool_role_found else '❌ 未找到'}")
    print("=" * 80)

if __name__ == "__main__":
    asyncio.run(test_sse_output())
