curl -s -X POST http://127.0.0.1:9099/30000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer *** \
  -d '{"model":"chatting","messages":[{"role":"user","content":"你好測試"}],"stream":true}' 2>&1 | grep "api-" | tail -5
