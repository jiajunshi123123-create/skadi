"""Enterprise数据AI Agent系统 - 集中配置管理"""
import os
from dotenv import load_dotenv

# 加载环境变量（云端部署时从 /opt/workspace/.env 读取）
_env_path = os.environ.get('ENV_FILE', '/opt/workspace/.env')
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
# 支持 mysql / postgresql / starrocks，通过环境变量切换
DB_TYPE = os.getenv('DB_TYPE', 'mysql')
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = int(os.getenv('DB_PORT', '3306'))
DB_USER = os.getenv('DB_USER', 'root')
DB_PASSWORD = os.getenv('DB_PASSWORD', '')
DB_NAME = os.getenv('DB_NAME', '')

# ============ [兼容] StarRocks 旧配置（已迁移至统一适配层） ============
# 保留用于兼容旧脚本，新代码请使用 DB_* 系列变量
STARROCKS_HOST = os.getenv('DB_HOST', 'your-starrocks-host')
STARROCKS_PORT = int(os.getenv('DB_PORT', '9030'))
STARROCKS_USER = os.getenv('DB_USER', 'your_starrocks_user')
STARROCKS_PASSWORD = os.getenv('DB_PASSWORD', os.getenv('STARROCKS_PASSWORD', ''))
STARROCKS_DB = os.getenv('DB_NAME', 'your_database')

# ============ PostgreSQL (经验知识库) ============
PG_HOST = os.getenv('PG_HOST', os.getenv('PGHOST', 'localhost'))
PG_PORT = int(os.getenv('PG_PORT', '5432'))
PG_DB = os.getenv('PG_DB', os.getenv('PGDATABASE', 'agent_experience'))
PG_USER = os.getenv('PG_USER', os.getenv('PGUSER', 'agent_user'))
PG_PASSWORD = os.getenv('PG_PASSWORD', os.getenv('PGPASSWORD', ''))

# ============ 路径配置 ============
PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prompts')
WORKSPACE_DIR = os.environ.get('WORKSPACE_DIR', '/opt/workspace')
SCRIPTS_DIR = os.path.join(WORKSPACE_DIR, 'scripts')
# [兼容] 旧subprocess方式的路径配置，新代码通过 DatabaseAdapter 直接连接
VENV_PYTHON = '/opt/venv/bin/python3'
STARROCKS_QUERY_SCRIPT = os.path.join(SCRIPTS_DIR, 'starrocks_query_safe.py')

# ============ 运行参数 ============
QUERY_TIMEOUT = 30          # SQL查询超时(秒)
MAX_SELF_HEAL_RETRIES = 2   # SQL自愈最大重试次数

# ============ 权限系统配置 ============
PERMISSION_CACHE_TTL = 300          # 角色缓存TTL(秒)
PERMISSION_DEFAULT_DENY = True      # 未配置用户是否默认拒绝
PERMISSION_AUDIT_ENABLED = True     # 是否启用权限审计日志
