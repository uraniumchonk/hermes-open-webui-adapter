#!/usr/bin/env python3
"""檢查 Open WebUI 的 api_type 設定和 Responses 模式行為"""

import asyncio
import json
import httpx

OWUI_URL = "http://127.0.0.1:30010"
EMAIL = "87000861+uraniumchonk@users.noreply.github.com"
PASSWORD = "Th19731112"

async def main():
    async with httpx.AsyncClient(timeout=15) as client:
        # ========== 登入 ==========
        print("1. 登入 Open WebUI")
        resp = await client.post(
            f"{OWUI_URL}/api/v1/auths/signin",
            json={"email": EMAIL, "password": PASSWORD},
        )
        if resp.status_code != 200:
            print(f"❌ 登入失敗: {resp.status_code}")
            return
        token = resp.json()["token"]
        h = {"Authorization": f"Bearer {token}"}
        print(f"✅ 登入成功, token: {token[:20]}...")
        
        # ========== 嘗試不同路徑找到 config ==========
        print("\n2. 搜尋正確的 API 端點")
        paths = [
            "/api/v1/openai/config",
            "/openai/config",
            "/api/openai/config",
            "/api/v1/config",
        ]
        for p in paths:
            resp = await client.get(f"{OWUI_URL}{p}", headers=h)
            try:
                j = resp.json()
                print(f"\n  ✅ {p}")
                if isinstance(j, dict):
                    for k in list(j.keys())[:10]:
                        v = j[k]
                        if isinstance(v, (list, dict)):
                            print(f"    {k}: {json.dumps(v, indent=4)[:300]}")
                        else:
                            print(f"    {k}: {v}")
            except:
                print(f"  ❌ {p}: status={resp.status_code} (not JSON)")
                # Check if it's HTML
                if resp.text[:100].strip().startswith("<"):
                    print(f"     (HTML response)")
                else:
                    print(f"     {resp.text[:100]}")
        
        # ========== 測試 Responses API 行為 ==========
        print("\n\n3. 測試: 完整歷史 vs 僅新 input")
        
        fh = {
            "Authorization": "Bearer hermes_chat_key",
            "Content-Type": "application/json",
        }
        
        # 完整歷史版本 (Open WebUI 當前行為)
        hist_payload = {
            "model": "qwen-27b-default",
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "第一輪對話:請記住這個數字42"}]},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "好的，我記住42了"}]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "第二輪對話:我剛說數字是多少?"}]},
            ],
            "stream": False,
        }
        print(f"\n  方式A: 完整歷史轉 input ({len(hist_payload['input'])} 條)")
        resp = await client.post("http://127.0.0.1:9099/30000/v1/responses", headers=fh, json=hist_payload)
        if resp.status_code == 200:
            r = resp.json()
            rid = r.get('id', 'N/A')
            for item in r.get('output', []):
                if item.get('type') == 'message':
                    for part in item.get('content', []):
                        print(f"    回覆: {part.get('text','')[:100]}")
            
            # stateful 版本 (只傳新 input + previous_response_id)
            stateful = {
                "model": "qwen-27b-default",
                "previous_response_id": rid,
                "input": [
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "第三輪對話:還記得之前說的數字嗎?"}]},
                ],
                "stream": False,
            }
            print(f"\n  方式B: 僅新 input + previous_response_id (1 條)")
            resp2 = await client.post("http://127.0.0.1:9099/30000/v1/responses", headers=fh, json=stateful)
            if resp2.status_code == 200:
                r2 = resp2.json()
                for item in r2.get('output', []):
                    if item.get('type') == 'message':
                        for part in item.get('content', []):
                            print(f"    回覆: {part.get('text','')[:100]}")
                print(f"  ✅ stateful 模式正常（記得數字42）")
            else:
                print(f"  ❌ stateful 失敗: {resp2.status_code}")
        else:
            print(f"  ❌ 錯誤: {resp.status_code}")

if __name__ == "__main__":
    asyncio.run(main())
