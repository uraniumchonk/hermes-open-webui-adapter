#!/usr/bin/env python3
"""
測試多個工具區塊的壓縮效果 - 驗證只壓縮最後一個
"""
import sys
sys.path.insert(0, '.')

from main import _compress_prev_action_blocks, _compress_details_tags

def test_multiple_blocks():
    """測試: 多個 PREV_ACTION 區塊，只壓縮最後一個"""
    content = """這是第一個工具調用

[START_PREV_ACTION]
[ACTION_TYPE]
search_files
[ACTION_ARG]
pattern: *.py
limit: 50
[RESULT]
total_count: 5
matches[0].path: main.py
matches[0].line: 42
matches[1].path: config.py
matches[1].line: 10
這是第一個工具的完整結果，應該被保留。
[END_PREV_ACTION]

這是第二個工具調用

[START_PREV_ACTION]
[ACTION_TYPE]
terminal
[ACTION_ARG]
command: ls -la
[RESULT]
total_count: 10
files[0]: main.py (1024 bytes)
files[1]: config.yaml (512 bytes)
files[2]: test.py (256 bytes)
files[3]: utils.py (128 bytes)
files[4]: app.py (2048 bytes)
這是第二個工具的完整結果，應該被保留。
[END_PREV_ACTION]

這是第三個工具調用（最新的）

[START_PREV_ACTION]
[ACTION_TYPE]
read_file
[ACTION_ARG]
path: /home/user/large_file.py
offset: 1
limit: 500
[RESULT]
1:import sys
2:import json
3:import asyncio
4:
5:class MyClass:
6:    def __init__(self):
7:        self.data = []
8:    
9:    def process(self, input_data):
10:        result = []
11:        for item in input_data:
12:            if item.get('valid'):
13:                result.append(item)
14:        return result
15:
16:def main():
17:    obj = MyClass()
18:    data = load_data()
19:    processed = obj.process(data)
20:    save_results(processed)
21:
22:if __name__ == '__main__':
23:    main()
這是第三個工具的完整結果，應該被壓縮。
[END_PREV_ACTION]

以上是所有工具結果。
"""
    
    print("=== 壓縮前 ===")
    print(f"總長度: {len(content)} 字元")
    blocks = content.count('[START_PREV_ACTION]')
    print(f"工具區塊數: {blocks}")
    print()
    
    compressed, count = _compress_prev_action_blocks(content, max_length=100)
    
    print("=== 壓縮後 ===")
    print(f"總長度: {len(compressed)} 字元")
    print(f"壓縮率: {(1 - len(compressed)/len(content)) * 100:.1f}%")
    print(f"壓縮區塊數: {count}")
    print()
    
    # 驗證第一個區塊被保留
    assert "matches[0].path: main.py" in compressed, "第一個區塊應該被保留"
    assert "matches[1].path: config.py" in compressed, "第一個區塊應該被保留"
    
    # 驗證第二個區塊被保留
    assert "files[0]: main.py" in compressed, "第二個區塊應該被保留"
    assert "files[1]: config.yaml" in compressed, "第二個區塊應該被保留"
    
    # 驗證第三個區塊被壓縮
    assert "(compressed from original" in compressed, "第三個區塊應該被壓縮"
    assert "1:import sys" not in compressed, "第三個區塊的內容應該被移除"
    
    print("✅ 測試通過: 只有最後一個區塊被壓縮")
    print()
    print("=== 壓縮後的內容 ===")
    print(compressed[:1000])
    print("...")

def test_multiple_details():
    """測試: 多個 details 標籤，只壓縮最後一個"""
    content = """第一個結果:
<details type="tool_calls">
<tool_code>search_files</tool_code>
<tool_args>pattern: *.py</tool_args>
<result>
{
  "total_count": 5,
  "files": [
    "/home/user/main.py",
    "/home/user/config.py"
  ]
}
</result>
</details>

第二個結果:
<details type="tool_calls">
<tool_code>terminal</tool_code>
<tool_args>command: ls</tool_args>
<result>
{
  "output": "main.py\nconfig.py\ntest.py",
  "exit_code": 0
}
</result>
</details>

第三個結果（最新）:
<details type="tool_calls">
<tool_code>read_file</tool_code>
<tool_args>path: large_file.py</tool_args>
<result>
{
  "content": "1|import sys\n2|import json\n3|import asyncio\n4|\n5:class MyClass:\n6|    def __init__(self):\n7|        self.data = []\n8|    \n9|    def process(self, input_data):\n10|        result = []\n11|        for item in input_data:\n12|            if item.get('valid'):\n13|                result.append(item)\n14|        return result\n15|\n16:def main():\n17|    obj = MyClass()\n18|    data = load_data()\n19|    processed = obj.process(data)\n20|    save_results(processed)\n21|\n22:if __name__ == '__main__':\n23|    main()",
  "total_lines": 23
}
</result>
</details>

以上是所有結果。
"""
    
    print("=== Details 標籤壓縮測試 ===")
    print(f"壓縮前長度: {len(content)} 字元")
    tags = content.count('<details type="tool_calls">')
    print(f"Details 標籤數: {tags}")
    print()
    
    compressed, count = _compress_details_tags(content, max_length=50)
    
    print(f"壓縮後長度: {len(compressed)} 字元")
    print(f"壓縮率: {(1 - len(compressed)/len(content)) * 100:.1f}%")
    print(f"壓縮標籤數: {count}")
    print()
    
    # 驗證前兩個標籤被保留
    assert '"total_count": 5' in compressed, "第一個標籤應該被保留"
    assert '"exit_code": 0' in compressed, "第二個標籤應該被保留"
    
    # 驗證第三個標籤被壓縮
    assert "truncated by [comp]" in compressed, "第三個標籤應該被壓縮"
    
    print("✅ 測試通過: 只有最後一個標籤被壓縮")

if __name__ == "__main__":
    test_multiple_blocks()
    print("\n" + "="*60 + "\n")
    test_multiple_details()
