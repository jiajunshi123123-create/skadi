"""Enterprise数据AI Agent系统 - 集中配置管理"""
import os

from dotenv import load_dotenv

from config.env_settings import read_setting

# 加载环境变量（云端部署时从 /opt/openclaw-workspace/.env 读取）
_env_path = os.environ.get('ENV_FILE', '/opt/openclaw-workspace/.env')
if os.path.exists(_env_path):
    load_dotenv(_env_path)
else:
    load_dotenv()  # 本地开发时从当前目录 .env 读取

# ============ DeepSeek API ============
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '')
DEEPSEEK_BASE_URL = 'https://api.deepseek.com/v1'

# ============ 模型配置 ============
PLAN_MODEL = os.getenv('PLAN_MODEL', 'deepseek-v4-pro')
QUERY_MODEL = os.getenv('QUERY_MODEL', 'deepseek-v4-flash')
ANALYSIS_MODEL = os.getenv('ANALYSIS_MODEL', 'deepseek-v4-pro')

PLAN_TEMPERATURE = 0.3      # SQL生成需要精确
QUERY_TEMPERATURE = 0.0     # 纯执行，零随机性
ANALYSIS_TEMPERATURE = 0.7  # 分析需要创造性

PLAN_MAX_TOKENS = 2000
QUERY_MAX_TOKENS = 1000
ANALYSIS_MAX_TOKENS = 3000

# ============ 数据源配置（统一适配层） ============
# 正式名字是 DB_*。STARROCKS_* 只作为弃用回退，读到时会打警告。
DB_TYPE = read_setting('DB_TYPE', default='mysql')
DB_HOST = read_setting('DB_HOST', 'STARROCKS_HOST', default='localhost')
DB_PORT = int(read_setting('DB_PORT', 'STARROCKS_PORT', default='3306'))
DB_USER = read_setting('DB_USER', 'STARROCKS_USER', default='')
DB_PASSWORD = read_setting('DB_PASSWORD', 'STARROCKS_PASSWORD', default='')
DB_NAME = read_setting('DB_NAME', 'STARROCKS_DB', default='')

# 旧代码仍从这些名字取值。数值已经按 DB_* 优先解析过，不再反向覆盖。
STARROCKS_HOST = DB_HOST
STARROCKS_PORT = DB_PORT
STARROCKS_USER = DB_USER
STARROCKS_PASSWORD = DB_PASSWORD
STARROCKS_DB = DB_NAME

# ============ PostgreSQL（经验知识库） ============
# 正式名字是 PG_DATABASE / PG_PASSWORD。PG_DB、PGDATABASE、PGPASSWORD、PGHOST 已弃用。
PG_HOST = read_setting('PG_HOST', 'PGHOST', default='localhost')
PG_PORT = int(read_setting('PG_PORT', 'PGPORT', default='5432'))
PG_DATABASE = read_setting('PG_DATABASE', 'PG_DB', 'PGDATABASE', default='agent_experience')
PG_DB = PG_DATABASE
PG_USER = read_setting('PG_USER', 'PGUSER', default='agent_user')
PG_PASSWORD = read_setting('PG_PASSWORD', 'PGPASSWORD', default='')

# ============ 路径配置 ============
PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prompts')
WORKSPACE_DIR = os.environ.get('WORKSPACE_DIR', '/opt/openclaw-workspace')
SCRIPTS_DIR = os.path.join(WORKSPACE_DIR, 'scripts')
# [兼容] 旧 subprocess 方式的路径配置，新代码通过 DatabaseAdapter 直接连接
VENV_PYTHON = '/opt/venv/bin/python3'
STARROCKS_QUERY_SCRIPT = os.path.join(SCRIPTS_DIR, 'starrocks_query_safe.py')

# ============ 运行参数 ============
QUERY_TIMEOUT = 30          # SQL查询超时（秒）
MAX_SELF_HEAL_RETRIES = 2   # SQL自愈最大重试次数

# ============ 权限系统配置 ============
PERMISSION_CACHE_TTL = 300          # 角色缓存 TTL（秒）
PERMISSION_DEFAULT_DENY = True      # 未配置用户是否默认拒绝
PERMISSION_AUDIT_ENABLED = True     # 是否启用权限审计日志

