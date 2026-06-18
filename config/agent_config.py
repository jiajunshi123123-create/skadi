"""Enterprise鏁版嵁AI Agent绯荤粺 - 闆嗕腑閰嶇疆绠＄悊"""
import os
from dotenv import load_dotenv

# 鍔犺浇鐜鍙橀噺锛堜簯绔儴缃叉椂浠?/opt/workspace/.env 璇诲彇锛?
_env_path = os.environ.get('ENV_FILE', '/opt/openclaw-workspace/.env')
if os.path.exists(_env_path):
    load_dotenv(_env_path)
else:
    load_dotenv()  # 鏈湴寮€鍙戞椂浠庡綋鍓嶇洰褰?.env 璇诲彇

# ============ DeepSeek API ============
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '')
DEEPSEEK_BASE_URL = 'https://api.deepseek.com/v1'

# ============ 妯″瀷閰嶇疆 ============
PLAN_MODEL = os.getenv('PLAN_MODEL', 'deepseek-v4-pro')
QUERY_MODEL = os.getenv('QUERY_MODEL', 'deepseek-v4-flash')
ANALYSIS_MODEL = os.getenv('ANALYSIS_MODEL', 'deepseek-v4-pro')

PLAN_TEMPERATURE = 0.3      # SQL鐢熸垚闇€瑕佺簿纭?
QUERY_TEMPERATURE = 0.0     # 绾墽琛岋紝闆堕殢鏈烘€?
ANALYSIS_TEMPERATURE = 0.7  # 鍒嗘瀽闇€瑕佸垱閫犳€?

PLAN_MAX_TOKENS = 2000
QUERY_MAX_TOKENS = 1000
ANALYSIS_MAX_TOKENS = 3000

# ============ 鏁版嵁婧愰厤缃紙缁熶竴閫傞厤灞傦級 ============
# 鏀寔 mysql / postgresql / starrocks锛岄€氳繃鐜鍙橀噺鍒囨崲
DB_TYPE = os.getenv('DB_TYPE', 'mysql')
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = int(os.getenv('DB_PORT', '3306'))
DB_USER = os.getenv('DB_USER', 'root')
DB_PASSWORD = os.getenv('DB_PASSWORD', '')
DB_NAME = os.getenv('DB_NAME', '')

# ============ [鍏煎] StarRocks 鏃ч厤缃紙宸茶縼绉昏嚦缁熶竴閫傞厤灞傦級 ============
# 淇濈暀鐢ㄤ簬鍏煎鏃ц剼鏈紝鏂颁唬鐮佽浣跨敤 DB_* 绯诲垪鍙橀噺
STARROCKS_HOST = os.getenv('DB_HOST', 'fe-c-76ef85649c2dc193.starrocks.aliyuncs.com')
STARROCKS_PORT = int(os.getenv('DB_PORT', '9030'))
STARROCKS_USER = os.getenv('DB_USER', 'WanWei_GJshijiajun')
STARROCKS_PASSWORD = os.getenv('DB_PASSWORD', os.getenv('STARROCKS_PASSWORD', ''))
STARROCKS_DB = os.getenv('DB_NAME', 'app_prod_db')

# ============ PostgreSQL (缁忛獙鐭ヨ瘑搴? ============
PG_HOST = os.getenv('PG_HOST', os.getenv('PGHOST', 'localhost'))
PG_PORT = int(os.getenv('PG_PORT', '5432'))
PG_DB = os.getenv('PG_DB', os.getenv('PGDATABASE', 'agent_experience'))
PG_USER = os.getenv('PG_USER', os.getenv('PGUSER', 'agent_user'))
PG_PASSWORD = os.getenv('PG_PASSWORD', os.getenv('PGPASSWORD', ''))

# ============ 璺緞閰嶇疆 ============
PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prompts')
WORKSPACE_DIR = os.environ.get('WORKSPACE_DIR', '/opt/openclaw-workspace')
SCRIPTS_DIR = os.path.join(WORKSPACE_DIR, 'scripts')
# [鍏煎] 鏃ubprocess鏂瑰紡鐨勮矾寰勯厤缃紝鏂颁唬鐮侀€氳繃 DatabaseAdapter 鐩存帴杩炴帴
VENV_PYTHON = '/opt/venv/bin/python3'
STARROCKS_QUERY_SCRIPT = os.path.join(SCRIPTS_DIR, 'starrocks_query_safe.py')

# ============ 杩愯鍙傛暟 ============
QUERY_TIMEOUT = 30          # SQL鏌ヨ瓒呮椂(绉?
MAX_SELF_HEAL_RETRIES = 2   # SQL鑷剤鏈€澶ч噸璇曟鏁?

# ============ 鏉冮檺绯荤粺閰嶇疆 ============
PERMISSION_CACHE_TTL = 300          # 瑙掕壊缂撳瓨TTL(绉?
PERMISSION_DEFAULT_DENY = True      # 鏈厤缃敤鎴锋槸鍚﹂粯璁ゆ嫆缁?
PERMISSION_AUDIT_ENABLED = True     # 鏄惁鍚敤鏉冮檺瀹¤鏃ュ織

