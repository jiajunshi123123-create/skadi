"""知识库初始化脚本

将数据字典、SQL模板、分析指南向量化写入 ChromaDB。

运行方式:
    cd /opt/workspace/data-agent  (云端)
    python3 -m knowledge.init_knowledge_base
或:
    python3 knowledge/init_knowledge_base.py
"""
import sys
import os

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.rag_tool import RAGTool


def init_data_dictionary(rag: RAGTool):
    """将数据字典写入知识库"""
    docs = [
        {
            'id': 'dict_value_daily',
            'content': (
                '表名: dwd_biz_bhv_maidian_user_event_value_daily\n'
                '用途: 全APP活跃用户查询\n'
                '分区键: dt_utc (DATETIME类型，注意不是dt)\n'
                '字段: dt_utc, user_id, raw_event_name, std_event_name, event_value\n'
                '口径: "活跃"、"日活"、"DAU"统一使用此表，COUNT(DISTINCT user_id)\n'
                '基线: 约34786 (2026-05-17)\n'
                '注意: 这是查询活跃用户的唯一正确数据源，禁止UNION其他表；\n'
                '事件字段是 raw_event_name(原始事件名)和 std_event_name(标准化事件名)，不是 event_name；\n'
                '日期过滤需使用 dt_utc >= \'YYYY-MM-DD\' AND dt_utc < \'次日\' 的左闭右开区间方式'
            ),
            'metadata': {'type': 'data_dictionary', 'table': 'value_daily'}
        },
        {
            'id': 'dict_core_behavior',
            'content': (
                '表名: dwd_biz_bhv_maidian_user_core_behavior_daily\n'
                '用途: 核心行为活跃用户查询（做题、阅读、打卡等）\n'
                '分区键: dt_utc (DATETIME类型，注意不是dt)\n'
                '字段: dt_utc, user_id, event_cnt, user_cnt\n'
                '基线: 约18862 (2026-05-17)\n'
                '注意: 完整表名含 dwd_biz_bhv_ 前缀；此表统计的是有核心行为的用户，不等于日活；\n'
                '日期过滤同样使用 dt_utc 左闭右开区间'
            ),
            'metadata': {'type': 'data_dictionary', 'table': 'core_behavior'}
        },
        {
            'id': 'dict_books_detail',
            'content': (
                '表名: dwd_biz_mrg_bhv_books_user_detail\n'
                '用途: 图书答疑活跃用户查询\n'
                '分区键: dt (DATETIME类型)\n'
                '字段: dt, uid(varchar，注意是uid不是user_id), books_id, name(varchar，书名，数据完整度99.9999%)\n'
                '基线: 约9121 (2026-05-17)\n'
                '注意: 用户ID字段名为uid (varchar类型)，与其他表不同；图书字段是books_id 不是book_id；本表无question_count字段；\n'
                '事实表内嵌书名字段name，查询图书明细/TOP/排行时必须在SELECT子句中包含 name as book_name，并在GROUP BY中含 books_id 与 name；禁止只返回 books_id 不带书名'
            ),
            'metadata': {'type': 'data_dictionary', 'table': 'books_detail'}
        },
        {
            'id': 'dict_books_dim',
            'content': (
                '表名: dwd_biz_ubb_prod_books_detail\n'
                '用途: 图书产品维度表（1516条记录），含图书完整元数据\n'
                '关键字段: id(图书ID), name(书名), subject_name(科目), grade_name(年级), version(版本), year(年份)\n'
                '使用场景: 需要图书年级/科目/版本等完整元数据时，可 LEFT JOIN dwd_biz_ubb_prod_books_detail d ON books_id = d.id\n'
                '注意: 仅需书名不需JOIN此表，事实表dwd_biz_mrg_bhv_books_user_detail已内嵌name字段；仅在查询需要科目/年级/版本等额外元数据时才引入此JOIN'
            ),
            'metadata': {'type': 'data_dictionary', 'table': 'books_dim'}
        },
        {
            'id': 'dict_new_user',
            'content': (
                '表名: dwd_biz_mrg_usr_new_user\n'
                '用途: 新增用户查询\n'
                '分区键: date (DATE类型，注意不是dt也不是dt_utc)\n'
                '字段: date, user_id\n'
                '基线: 约13167 (2026-05-17)\n'
                '注意: 分区键名为 date，不是 dt；本表无 channel/platform/created_time 字段，新用户以 date 分区为准'
            ),
            'metadata': {'type': 'data_dictionary', 'table': 'new_user'}
        }
    ]

    ids = [d['id'] for d in docs]
    documents = [d['content'] for d in docs]
    metadatas = [d['metadata'] for d in docs]
    rag.add_documents(ids, documents, metadatas)
    print(f'  [数据字典] 已写入 {len(docs)} 条文档')


def init_sql_templates(rag: RAGTool):
    """将常用SQL模板写入知识库"""
    templates = [
        {
            'id': 'tpl_daily_active',
            'content': (
                '查询模式: 查询某天的日活/活跃用户数\n'
                "SQL模板: SELECT COUNT(DISTINCT user_id) as dau "
                "FROM dwd_biz_bhv_maidian_user_event_value_daily "
                "WHERE dt_utc >= '{date}' AND dt_utc < '{next_date}'\n"
                '注意: 分区键是 dt_utc(DATETIME)，使用左闭右开区间，不要用 dt = \'YYYY-MM-DD\'\n'
                '适用问题: "昨天日活多少"、"今天活跃用户"、"X月X日DAU"'
            ),
            'metadata': {'type': 'sql_template', 'pattern': 'daily_active'}
        },
        {
            'id': 'tpl_active_trend',
            'content': (
                '查询模式: 查询一段时间内日活趋势\n'
                "SQL模板: SELECT DATE(dt_utc) as d, COUNT(DISTINCT user_id) as dau "
                "FROM dwd_biz_bhv_maidian_user_event_value_daily "
                "WHERE dt_utc >= '{start_date}' AND dt_utc < '{end_date_exclusive}' "
                "GROUP BY DATE(dt_utc) ORDER BY d\n"
                '注意: 分区键 dt_utc 为 DATETIME，按天聚合需 DATE(dt_utc)\n'
                '适用问题: "最近7天日活趋势"、"本周活跃变化"'
            ),
            'metadata': {'type': 'sql_template', 'pattern': 'active_trend'}
        },
        {
            'id': 'tpl_new_users',
            'content': (
                '查询模式: 查询新增用户数\n'
                "SQL模板: SELECT COUNT(DISTINCT user_id) as new_users "
                "FROM dwd_biz_mrg_usr_new_user WHERE date = '{date}'\n"
                '注意: 此表分区键为 date(DATE类型)，不是 dt 也不是 dt_utc\n'
                '适用问题: "昨天新增多少用户"、"今日注册数"'
            ),
            'metadata': {'type': 'sql_template', 'pattern': 'new_users'}
        },
        {
            'id': 'tpl_core_behavior',
            'content': (
                '查询模式: 查询核心行为活跃用户\n'
                "SQL模板: SELECT COUNT(DISTINCT user_id) as core_active "
                "FROM dwd_biz_bhv_maidian_user_core_behavior_daily "
                "WHERE dt_utc >= '{date}' AND dt_utc < '{next_date}'\n"
                '注意: 完整表名含 dwd_biz_bhv_ 前缀；分区键是 dt_utc(DATETIME)，左闭右开\n'
                '适用问题: "核心活跃用户数"、"有多少用户做了核心行为"'
            ),
            'metadata': {'type': 'sql_template', 'pattern': 'core_behavior'}
        },
        {
            'id': 'tpl_books_active',
            'content': (
                '查询模式: 查询图书答疑活跃用户\n'
                "SQL模板（全量活跃数）: SELECT COUNT(DISTINCT uid) as books_active "
                "FROM dwd_biz_mrg_bhv_books_user_detail "
                "WHERE dt >= '{date}' AND dt < '{next_date}'\n"
                "SQL模板（含书名的活跃排行）: SELECT books_id, name as book_name, COUNT(DISTINCT uid) as active_users "
                "FROM dwd_biz_mrg_bhv_books_user_detail "
                "WHERE dt >= '{start_date}' AND dt < '{end_date}' "
                "GROUP BY books_id, name ORDER BY active_users DESC LIMIT 20\n"
                '注意: 用户字段是 uid(varchar) 不是 user_id；分区键 dt 为 DATETIME；图书字段是 books_id 不是 book_id；本表无 question_count；\n'
                '事实表内嵌 name (书名) 字段，查询图书明细/TOP/排行时 SELECT 必须包含 name，GROUP BY 同时包含 books_id 与 name，禁止只返回 books_id\n'
                '适用问题: "图书答疑活跃"、"图书用户数"、"哪本书最热门"、"图书活跃排行"'
            ),
            'metadata': {'type': 'sql_template', 'pattern': 'books_active'}
        },
        {
            'id': 'tpl_books_with_dim',
            'content': (
                '查询模式: 按科目/年级/版本维度统计图书答疑活跃（需JOIN维度表）\n'
                "SQL模板: SELECT b.books_id, b.name as book_name, d.subject_name, d.grade_name, "
                "COUNT(DISTINCT b.uid) as active_users "
                "FROM dwd_biz_mrg_bhv_books_user_detail b "
                "LEFT JOIN dwd_biz_ubb_prod_books_detail d ON b.books_id = d.id "
                "WHERE b.dt >= '{start_date}' AND b.dt < '{end_date}' "
                "GROUP BY b.books_id, b.name, d.subject_name, d.grade_name "
                "ORDER BY active_users DESC LIMIT 20\n"
                '注意: 仅需书名不需JOIN维度表，事实表已内嵌 name；仅当需要科目、年级、版本、年份等额外元数据时才 LEFT JOIN dwd_biz_ubb_prod_books_detail\n'
                '适用问题: "按科目看图书活跃"、"哪个年级的书最热门"、"人教版书使用情况"'
            ),
            'metadata': {'type': 'sql_template', 'pattern': 'books_with_dim'}
        },
        {
            'id': 'tpl_compare_metrics',
            'content': (
                '查询模式: 对比不同指标\n'
                '说明: 需要分别查询各个表，不要UNION\n'
                '示例: 先查 value_daily(dt_utc) 得到日活，再查 core_behavior(dt_utc) 得到核心活跃，'
                '在分析层面进行对比\n'
                '适用问题: "日活和核心活跃对比"、"各类活跃对比"'
            ),
            'metadata': {'type': 'sql_template', 'pattern': 'compare'}
        },
        {
            'id': 'tpl_event_filter',
            'content': (
                '查询模式: 按事件名称过滤活跃用户\n'
                "SQL模板: SELECT COUNT(DISTINCT user_id) as event_users "
                "FROM dwd_biz_bhv_maidian_user_event_value_daily "
                "WHERE dt_utc >= '{date}' AND dt_utc < '{next_date}' "
                "AND std_event_name = '{event}'\n"
                '注意: 事件字段是 raw_event_name(原始)和 std_event_name(标准化)，不是 event_name\n'
                '适用问题: "做题事件用户数"、"某事件触发人数"'
            ),
            'metadata': {'type': 'sql_template', 'pattern': 'event_filter'}
        }
    ]

    ids = [t['id'] for t in templates]
    documents = [t['content'] for t in templates]
    metadatas = [t['metadata'] for t in templates]
    rag.add_documents(ids, documents, metadatas)
    print(f'  [SQL模板] 已写入 {len(templates)} 条文档')


def init_analysis_guidelines(rag: RAGTool):
    """将分析指南写入知识库"""
    guidelines = [
        {
            'id': 'guide_trend_analysis',
            'content': (
                '分析指南: 趋势分析\n'
                '当数据呈现上升趋势时:\n'
                '- 检查是否有新功能上线（产品因素）\n'
                '- 检查是否有市场活动（推广因素）\n'
                '- 检查是否是工作日/周末效应（周期因素）\n'
                '当数据呈现下降趋势时:\n'
                '- 检查是否有bug或故障（技术因素）\n'
                '- 检查是否是假期/淡季（时间因素）\n'
                '- 检查是否有竞品动态（市场因素）'
            ),
            'metadata': {'type': 'analysis_guide', 'topic': 'trend'}
        },
        {
            'id': 'guide_anomaly',
            'content': (
                '分析指南: 异常检测\n'
                '基线标准:\n'
                '- 全APP日活基线: ~34786, 波动>20%视为异常\n'
                '- 核心活跃基线: ~18862, 波动>20%视为异常\n'
                '- 图书答疑基线: ~9121, 波动>25%视为异常\n'
                '- 新增用户基线: ~13167, 波动>30%视为异常\n'
                '异常处理: 发现异常时必须给出可能原因和建议排查方向'
            ),
            'metadata': {'type': 'analysis_guide', 'topic': 'anomaly'}
        },
        {
            'id': 'guide_caliber',
            'content': (
                '口径规范:\n'
                '1. "活跃"/"日活"/"DAU" → 必须用 dwd_biz_bhv_maidian_user_event_value_daily 表，COUNT(DISTINCT user_id)\n'
                '2. 禁止UNION四分类表来算活跃\n'
                '3. 图书表(dwd_biz_mrg_bhv_books_user_detail)用户字段名是 uid(varchar) 不是 user_id；图书字段是 books_id 不是 book_id；事实表内嵌 name(书名)字段，查询图书明细/TOP/排行时 SELECT 必须包含 name，禁止只返回 books_id 不带书名\n'
                '4. 新用户表(dwd_biz_mrg_usr_new_user)分区键为 date(DATE)，无 channel/platform/created_time 字段\n'
                '5. 分区键差异：value_daily 与 core_behavior_daily 用 dt_utc(DATETIME)；books_user_detail 用 dt(DATETIME)；new_user 用 date(DATE)\n'
                '6. DATETIME 类型分区键应使用 dt_utc >= \'YYYY-MM-DD\' AND dt_utc < \'次日\' 的左闭右开区间过滤\n'
                '7. 事件字段是 raw_event_name / std_event_name，不存在 event_name 字段\n'
                '8. 核心行为表完整表名为 dwd_biz_bhv_maidian_user_core_behavior_daily(含 dwd_biz_bhv_ 前缀)\n'
                '9. 数据是只读的，禁止 INSERT/UPDATE/DELETE'
            ),
            'metadata': {'type': 'analysis_guide', 'topic': 'caliber'}
        }
    ]

    ids = [g['id'] for g in guidelines]
    documents = [g['content'] for g in guidelines]
    metadatas = [g['metadata'] for g in guidelines]
    rag.add_documents(ids, documents, metadatas)
    print(f'  [分析指南] 已写入 {len(guidelines)} 条文档')


def main():
    """初始化知识库主函数"""
    print('=== 初始化Enterprise数据AI知识库 ===')
    print(f'存储路径: {RAGTool().db_path}')
    print()

    rag = RAGTool()

    # 清理已有数据（重新初始化）
    try:
        rag.client.delete_collection('enterprise_knowledge')
        print('[清理] 已删除旧知识库集合')
    except Exception:
        print('[清理] 无旧集合需要清理')

    print()
    init_data_dictionary(rag)
    init_sql_templates(rag)
    init_analysis_guidelines(rag)

    # 验证
    collection = rag.get_or_create_collection()
    total = collection.count()
    print(f'\n=== 知识库初始化完成 ===')
    print(f'总文档数: {total}')

    # 测试检索
    print('\n--- 检索测试 ---')
    test_queries = [
        '日活用户怎么查',
        '图书答疑',
        '数据异常怎么分析',
    ]
    for q in test_queries:
        results = rag.search(q, n_results=2)
        print(f'  查询: "{q}" → 返回 {len(results)} 条结果')
        if results:
            print(f'    最佳匹配: {results[0]["content"][:60]}...')


if __name__ == '__main__':
    main()
