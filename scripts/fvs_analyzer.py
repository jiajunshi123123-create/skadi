"""
FVS 看板通用解析器 v2.0
用法: python fvs_analyzer.py <fvs文件路径> [--json 输出文件]
输出: 终端打印完整分析报告 + JSON结果文件
"""
import json, re, os, sys, zipfile, tempfile, shutil, io

# Fix Windows GBK encoding for emoji
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from collections import defaultdict

def parse_fvs(fvs_path):
    """解析单个FVS文件，返回结构化分析结果"""
    result = {
        "file": os.path.basename(fvs_path),
        "dashboard_name": "",
        "version": "",
        "size": "",
        "theme": "",
        "filters": [],
        "workbooks": [],      # 指标卡
        "charts": [],         # 图表
        "datasets": defaultdict(list),  # 数据集名 → [使用它的组件]
        "dimensions": defaultdict(list), # 维度字段 → [使用它的组件]
        "warnings": []        # 发现的问题
    }
    
    tmpdir = tempfile.mkdtemp()
    try:
        # ---- 1. 解压 ----
        with zipfile.ZipFile(fvs_path, 'r') as zf:
            zf.extractall(tmpdir)
        
        # ---- 2. 解析 store.json ----
        store_path = os.path.join(tmpdir, 'store.json')
        if not os.path.exists(store_path):
            result["warnings"].append("未找到store.json")
            return result
        
        with open(store_path, 'r', encoding='utf-8') as f:
            store = json.load(f)
        
        result["dashboard_name"] = store.get("name", "未知")
        result["version"] = store.get("templateVersion", "")
        result["size"] = f"{store.get('width','?')}x{store.get('height','?')}"
        
        # 主题
        sc = store.get("styleConfig", {})
        if isinstance(sc, dict):
            theme_cfg = sc.get("theme_config", {})
            if isinstance(theme_cfg, dict):
                result["theme"] = theme_cfg.get("themeName", "")
        
        # 筛选器
        params = store.get("parameterConfigs", [])
        for p in params:
            if isinstance(p, dict):
                result["filters"].append(p.get("name", p.get("widgetName", "?")))
        
        # 提取筛选器数据集信息
        computes = store.get("computes", {})
        for cid, cval in computes.items():
            if not isinstance(cval, dict):
                continue
            if cval.get("type") == "dataset":
                ds_name = cval.get("datasetName", "")
                cols = [c.get("name", "?") for c in cval.get("columns", [])]
                if ds_name and ds_name not in result["filters"]:
                    result["filters"].append(f"{ds_name}({','.join(cols)})")
        
        # ---- 3. 解析 .chart 文件 ----
        for fname in os.listdir(tmpdir):
            if not fname.endswith('.chart'):
                continue
            chart_path = os.path.join(tmpdir, fname)
            with open(chart_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            
            xml_start = content.find('<?xml')
            if xml_start < 0:
                continue
            content = content[xml_start:]
            
            # 图表类型
            plot_match = re.search(r'Plot class="[^"]*Chart(\w+)', content)
            chart_type = plot_match.group(1) if plot_match else "?"
            chart_type = chart_type.replace("Plot", "").replace("4VanChart", "")
            
            # 数据集名
            ds_match = re.search(r'<Name>\s*<!\[CDATA\[(.*?)\]\]>\s*</Name>', content, re.DOTALL)
            dataset_name = ds_match.group(1).strip() if ds_match else "?"
            
            # 维度
            cat_match = re.search(r'<CategoryName value="([^"]*)"', content)
            dim = cat_match.group(1) if cat_match else ""
            
            # 指标
            metrics = []
            for m in re.finditer(r'<ChartSummaryColumn name="([^"]*)" function="[^"]*\.(\w+)" customName="([^"]*)"', content):
                metrics.append({"field": m.group(1), "function": m.group(2), "label": m.group(3)})
            # OneValue
            ov = re.search(r'seriesName="([^"]*)" valueName="([^"]*)" function="[^"]*\.(\w+)"', content)
            if ov:
                metrics.append({"field": ov.group(2), "function": ov.group(3), "label": ov.group(1)})
            
            chart_info = {
                "id": fname.replace('.chart', '')[:8],
                "type": chart_type,
                "dataset": dataset_name,
                "dimension": dim,
                "metrics": metrics
            }
            result["charts"].append(chart_info)
            result["datasets"][dataset_name].append(f"chart:{chart_type}")
            if dim:
                result["dimensions"][dim].append(f"chart:{dataset_name}")
        
        # ---- 4. 解析 .ec 文件（指标卡） ----
        for fname in os.listdir(tmpdir):
            if not fname.endswith('.ec'):
                continue
            ec_path = os.path.join(tmpdir, fname)
            with open(ec_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            
            xml_start = content.find('<?xml')
            if xml_start < 0:
                continue
            content = content[xml_start:]
            
            # 字段
            cols = re.findall(r'columnName="([^"]*)"', content)
            # 公式
            formulas = re.findall(r'<Formula>(.*?)</Formula>', content, re.DOTALL)
            
            if cols:
                wb_info = {
                    "id": fname.replace('.ec', '')[:8],
                    "fields": list(set(cols)),
                    "has_period_compare": any('datedefined' in f or 'D8<' in f or 'G8>' in f for f in formulas)
                }
                result["workbooks"].append(wb_info)
        
        # ---- 5. 冗余检测 ----
        # 5a. 同名数据集被多图表使用
        for ds_name, users in result["datasets"].items():
            chart_users = [u for u in users if u.startswith("chart:")]
            if len(chart_users) > 1:
                result["warnings"].append(
                    f"🔴 数据集'{ds_name}'被{len(chart_users)}个独立图表使用 → 建议合并为多系列图表"
                )
        
        # 5b. 维度字段命名不一致
        dim_names = list(result["dimensions"].keys())
        if len(dim_names) > 1:
            # 检查是否有关联的维度（如 provice vs province_name）
            dim_lower = defaultdict(list)
            for d in dim_names:
                key = d.lower().replace('_', '').replace('-', '')
                dim_lower[key].append(d)
            for key, variants in dim_lower.items():
                if len(variants) > 1:
                    result["warnings"].append(
                        f"🔴 维度字段命名不一致: {variants} → 建议统一为一个字段名"
                    )
        
        # 5c. 同业务域指标卡冗余提示
        domain_keywords = {
            "答疑": ["答疑", "dayi", "answer"],
            "图书": ["图书", "book", "激活"],
            "会员": ["会员", "member", "开通"],
            "提分": ["提分", "tifen", "学习"],
            "用户": ["user_cnt", "open_cnt", "json_cnt", "注册", "活跃", "游客"]
        }
        for domain, keywords in domain_keywords.items():
            related = [w for w in result["workbooks"] 
                       if any(kw in str(w.get("fields", [])).lower() for kw in keywords)]
            if len(related) >= 3:
                result["warnings"].append(
                    f"🟡 {domain}域有{len(related)}个指标卡 → 建议整合为漏斗卡组"
                )
        
        return result
    
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def print_report(result):
    """打印分析报告"""
    print("=" * 70)
    print(f"📊 看板分析报告: {result['dashboard_name']}")
    print("=" * 70)
    print(f"  文件: {result['file']}")
    print(f"  版本: {result['version']}")
    print(f"  尺寸: {result['size']}")
    print(f"  主题: {result['theme']}")
    
    print(f"\n📌 筛选器 ({len(result['filters'])}):")
    for f in result['filters']:
        print(f"  - {f}")
    
    print(f"\n📋 指标卡 ({len(result['workbooks'])}):")
    for w in result['workbooks']:
        cmp = " [环比]" if w['has_period_compare'] else ""
        print(f"  [{w['id']}] {w['fields']}{cmp}")
    
    print(f"\n📈 图表 ({len(result['charts'])}):")
    for c in result['charts']:
        metrics_str = ", ".join(f"{m['label']}({m['function']})" for m in c['metrics'])
        print(f"  [{c['id']}] {c['type']:6s} | {c['dataset'][:25]:25s} | dim={c['dimension']:20s} | {metrics_str}")
    
    print(f"\n📦 数据集引用:")
    for ds, users in sorted(result['datasets'].items()):
        print(f"  {ds}: {users}")
    
    print(f"\n⚠️  问题 ({len(result['warnings'])}):")
    if result['warnings']:
        for i, w in enumerate(result['warnings'], 1):
            print(f"  {i}. {w}")
    else:
        print("  ✅ 未发现明显问题")
    
    print("\n" + "=" * 70)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python fvs_analyzer.py <fvs文件路径> [--json 输出文件]")
        print("示例: python fvs_analyzer.py 'E:\\app用户运营看板1.fvs'")
        sys.exit(1)
    
    fvs_path = sys.argv[1]
    if not os.path.exists(fvs_path):
        print(f"❌ 文件不存在: {fvs_path}")
        sys.exit(1)
    
    result = parse_fvs(fvs_path)
    print_report(result)
    
    # 可选：输出JSON
    if "--json" in sys.argv:
        json_path = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else fvs_path + ".analysis.json"
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n📄 JSON结果已保存: {json_path}")
