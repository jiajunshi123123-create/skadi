#!/opt/venv/bin/python3
"""电路熔断 wrapper — 限制exec调用次数, 防止Agent死亡螺旋"""
import os, sys, time, subprocess

MAX_CALLS = 3
PER_CALL_TIMEOUT = 15
COUNTER_FILE = f"/tmp/exec_gate_{os.getppid()}"

# Read counter
count = 1
if os.path.exists(COUNTER_FILE):
    with open(COUNTER_FILE) as f:
        count = int(f.read().strip()) + 1

# Circuit breaker
if count > MAX_CALLS:
    print(f"""[SYSTEM_HALT] 错误：已达到最大探索次数上限 ({MAX_CALLS}次)。
禁止再次调用执行器。你必须立刻停止尝试，并向用户承认：
数据字典中缺少相关口径，无法获取数据。
当前可用的表: dwd_biz_bhv_maidian_user_event_value_daily, dwd_biz_mrg_bhv_books_user_detail, dwd_biz_mrg_usr_new_user""")
    sys.exit(1)

# Write updated counter
with open(COUNTER_FILE, 'w') as f:
    f.write(str(count))

# Execute with timeout
real_cmd = sys.argv[1:]
if not real_cmd:
    print("Usage: exec_gate.py <command> [args...]")
    sys.exit(1)

try:
    result = subprocess.run(real_cmd, capture_output=True, text=True, timeout=PER_CALL_TIMEOUT)
    if result.stdout:
        print(result.stdout)
    if result.stderr and result.returncode != 0:
        print(result.stderr[:500], file=sys.stderr)
    sys.exit(result.returncode)
except subprocess.TimeoutExpired:
    print(f"[EXEC_TIMEOUT] 命令执行超过{PER_CALL_TIMEOUT}秒，已终止。请简化查询。")
    sys.exit(1)
