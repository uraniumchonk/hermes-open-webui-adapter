import subprocess
import json

result = subprocess.run([
    "curl", "-s", "-X", "POST",
    "http://127.0.0.1:9099/30000/v1/chat/completions",
    "-H", "Content-Type: application/json",
    "-H", "Authorization: Bearer ***",
    "-d", json.dumps({
        "model": "chatting",
        "messages": [{"role": "user", "content": "你好測試"}],
        "stream": True
    })
], capture_output=True, text=True)

# Find the marker chunk
for line in result.stdout.split('\n'):
    if 'api-' in line and 'u200b' in line:
        print("✅ Marker found:")
        print(line)
        break
else:
    print("❌ Marker NOT found")
    print("Last 5 lines:")
    for line in result.stdout.split('\n')[-5:]:
        print(line)
