#!/usr/bin/env python3
"""
企业级多法人实体内部交易对账与合并报表自动化系统 - 主入口CLI

用法:
  python main.py init                          # 初始化数据库
  python main.py scheduler                     # 启动定时调度器
  python main.py fetch --date 2025-06-01       # 手动触发交易抓取
  python main.py match --date 2025-06-01       # 手动触发交易匹配
  python main.py match-concurrent --date 2025-06-01  # 并发匹配
  python main.py import-tb --file data.csv --company SUB01 --period 2025-06  # 导入试算平衡表
  python main.py validate --period 2025-06     # 校验试算平衡
  python main.py consolidate --period 2025-06  # 生成合并工作底稿和抵消分录
  python main.py report --period 2025-06 --format excel  # 生成合并报表
  python main.py report --period 2025-06 --format pdf
  python main.py quarterly --year 2025 --quarter 2       # 生成季度报告
  python main.py check-timeouts                # 检查超时工单
  python main.py check-late --period 2025-06   # 检查延迟提交
  python main.py check-anomalies               # 检查异常
  python main.py add-elimination --period 2025-06 --amount 6000000  # 手工抵消分录
  python main.py add-subsidiary --code SUB01 --name "子公司A" --ratio 0.8  # 添加子公司
  python main.py list-subsidiaries             # 列出子公司
  python main.py list-workorders [--status open]  # 查询工单
  python main.py demo                          # 生成演示数据
"""

import argparse
import sys
import os
import json
from datetime import date, datetime, timedelta
from decimal import Decimal

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)


def cmd_init(args):
    from models import init_db
    init_db()
    print("数据库初始化完成")


def cmd_scheduler(args):
    from scheduler import TaskScheduler
    ts = TaskScheduler()
    ts.start()


def cmd_fetch(args):
    from reconciliation import TransactionFetcher
    from notification import setup_logging
    setup_logging()
    target_date = date.fromisoformat(args.date) if args.date else date.today() - timedelta(days=1)
    fetcher = TransactionFetcher()
    results = fetcher.daily_fetch_all(target_date)
    print(f"抓取结果: {json.dumps(results, ensure_ascii=False, indent=2)}")


def cmd_match(args):
    from reconciliation import TransactionMatcher
    from notification import setup_logging
    setup_logging()
    target_date = date.fromisoformat(args.date) if args.date else date.today() - timedelta(days=1)
    matcher = TransactionMatcher()
    result = matcher.match_batch(transaction_date=target_date)
    print(f"匹配结果: {json.dumps(result, ensure_ascii=False, indent=2)}")


def cmd_match_concurrent(args):
    from reconciliation import TransactionMatcher
    from notification import setup_logging
    setup_logging()
    target_date = date.fromisoformat(args.date) if args.date else date.today() - timedelta(days=1)
    matcher = TransactionMatcher()
    result = matcher.match_concurrent(transaction_date=target_date)
    print(f"并发匹配结果: {json.dumps(result, ensure_ascii=False, indent=2)}")


def cmd_import_tb(args):
    from trial_balance import TrialBalanceManager
    from notification import setup_logging
    setup_logging()
    mgr = TrialBalanceManager()
    if args.file.endswith(".csv"):
        saved, unbalanced = mgr.import_from_csv(args.file, args.company, args.period)
    elif args.file.endswith((".xlsx", ".xls")):
        saved, unbalanced = mgr.import_from_excel(args.file, args.company, args.period)
    else:
        print(f"不支持的文件格式: {args.file}")
        return
    print(f"导入完成: {saved} 条, 不平衡 {unbalanced} 条")


def cmd_validate(args):
    from trial_balance import TrialBalanceManager
    from notification import setup_logging
    setup_logging()
    mgr = TrialBalanceManager()
    results = mgr.validate_all_companies(args.period)
    for r in results:
        status = "✓ 平衡" if r["is_balanced"] else "✗ 不平衡"
        print(f"  {r['company_code']} ({r['period']}): 借方 {r['total_debit']:,.2f} "
              f"贷方 {r['total_credit']:,.2f} 差额 {r['imbalance']:,.2f} {status}")
        if r["unbalanced_accounts"] > 0:
            for d in r["unbalanced_details"][:5]:
                print(f"    - {d['account_code']} {d['account_name']}: 差额 {d['imbalance']:,.2f}")


def cmd_consolidate(args):
    from consolidation import ConsolidationEngine
    from notification import setup_logging
    setup_logging()
    engine = ConsolidationEngine()
    print(f"生成合并工作底稿: {args.period}")
    ws_result = engine.generate_worksheet(args.period)
    print(f"  工作底稿: {ws_result['total_rows']} 行, "
          f"原始合计 {ws_result['total_original']:,.2f}, "
          f"调整合计 {ws_result['total_adjustment']:,.2f}, "
          f"合并合计 {ws_result['total_consolidated']:,.2f}")

    print(f"生成抵消分录: {args.period}")
    elim_results = engine.generate_elimination_entries(args.period)
    print(f"  自动抵消分录: {len(elim_results)} 条")

    minority = engine.calculate_minority_interest(args.period)
    for m in minority:
        print(f"  少数股东: {m['company_name']} 权益 {m['minority_equity']:,.2f} "
              f"损益 {m['minority_profit']:,.2f}")


def cmd_report(args):
    from report_generator import ReportGenerator
    from notification import setup_logging
    setup_logging()
    gen = ReportGenerator()
    fmt = args.format if args.format else "all"

    if fmt in ("excel", "all"):
        path = gen.export_to_excel(args.period, "all")
        print(f"Excel导出: {path}")

    if fmt in ("pdf", "all"):
        path = gen.export_to_pdf(args.period, "all")
        print(f"PDF导出: {path}")


def cmd_quarterly(args):
    from quarterly_analyzer import QuarterlyAnalyzer
    from notification import setup_logging
    setup_logging()
    analyzer = QuarterlyAnalyzer()
    result = analyzer.generate_quarterly_report(args.year, args.quarter)
    print(f"=== {args.year}年第{args.quarter}季度对账分析报告 ===")
    print(f"  总交易笔数: {result['total_transactions']}")
    print(f"  匹配笔数: {result['matched_transactions']}")
    print(f"  差异工单数: {result['discrepancy_count']}")
    print(f"  对账完成率: {result['reconciliation_completion_rate']}%")
    print(f"  差异率: {result['discrepancy_rate']}%")
    print(f"  平均处理时长: {result['avg_processing_hours']}小时")

    trend = result.get("trend", {})
    if trend.get("description"):
        print(f"  趋势: {trend['description']}")

    filepath = analyzer.export_quarterly_report_excel(args.year, args.quarter)
    print(f"季度报告Excel: {filepath}")


def cmd_check_timeouts(args):
    from notification import EscalationService, setup_logging
    setup_logging()
    svc = EscalationService()
    escalated = svc.check_and_escalate_work_orders()
    print(f"超时升级: {len(escalated)} 个工单")
    for wid in escalated:
        print(f"  - {wid}")


def cmd_check_late(args):
    from notification import EscalationService, setup_logging
    setup_logging()
    svc = EscalationService()
    late_list = svc.check_and_remind_late_submissions(args.period)
    print(f"延迟提交: {len(late_list)} 家子公司")
    for item in late_list:
        escalate = " [需升级]" if item["should_escalate"] else ""
        print(f"  - {item['company_name']}({item['company_code']}) 催办{item['reminder_count']}次{escalate}")


def cmd_check_anomalies(args):
    from notification import EscalationService, setup_logging
    setup_logging()
    svc = EscalationService()

    long_unresolved = svc.check_long_unresolved(days_threshold=7)
    print(f"长期未处理工单: {len(long_unresolved)} 个")
    for item in long_unresolved:
        print(f"  - 工单{item['work_order_id']}: {item['days_open']}天, "
              f"差异金额 {item['discrepancy_amount']:,.2f}")

    high_discrepancy = svc.check_high_discrepancy(amount_threshold=1000000)
    print(f"高差异工单: {len(high_discrepancy)} 个")
    for item in high_discrepancy:
        print(f"  - 工单{item['work_order_id']}: 差异 {item['discrepancy_amount']:,.2f}")


def cmd_add_elimination(args):
    from consolidation import ConsolidationEngine
    from notification import setup_logging
    setup_logging()
    engine = ConsolidationEngine()
    entry_data = {
        "entry_type": "manual_adjustment",
        "debit_company": args.debit_company or "",
        "debit_account": args.debit_account or "",
        "credit_company": args.credit_company or "",
        "credit_account": args.credit_account or "",
        "amount": args.amount,
        "description": args.description or "手工调整抵消分录",
    }
    entry = engine.add_manual_elimination_entry(args.period, entry_data, args.created_by or "admin")
    print(f"手工抵消分录已创建: ID={entry.id}, 金额={entry.amount}")
    if entry.requires_approval:
        print(f"  ⚠ 金额超500万元, 触发三级审批")


def cmd_add_subsidiary(args):
    from models import Subsidiary, SessionLocal
    from notification import setup_logging
    setup_logging()
    session = SessionLocal()
    sub = Subsidiary(
        id=str(os.urandom(16).hex()),
        name=args.name,
        code=args.code,
        ownership_ratio=Decimal(str(args.ratio)),
        cfo_name=args.cfo or "",
        finance_staff=args.staff or "",
        is_active=True,
    )
    session.add(sub)
    session.commit()
    print(f"子公司已添加: {sub.name}({sub.code}), 持股比例 {sub.ownership_ratio}")


def cmd_list_subsidiaries(args):
    from models import Subsidiary, SessionLocal
    from notification import setup_logging
    setup_logging()
    session = SessionLocal()
    subs = session.query(Subsidiary).filter_by(is_active=True).all()
    if not subs:
        print("暂无子公司数据")
        return
    print(f"{'编码':<10} {'名称':<20} {'持股比例':<10} {'CFO':<10}")
    print("-" * 55)
    for sub in subs:
        print(f"{sub.code:<10} {sub.name:<20} {float(sub.ownership_ratio):<10.1%} {sub.cfo_name or '-':<10}")


def cmd_list_workorders(args):
    from models import DiscrepancyWorkOrder, WorkOrderStatus, SessionLocal
    from notification import setup_logging
    setup_logging()
    session = SessionLocal()
    query = session.query(DiscrepancyWorkOrder)
    if args.status:
        try:
            status_enum = WorkOrderStatus(args.status)
            query = query.filter_by(status=status_enum)
        except ValueError:
            print(f"无效状态: {args.status}, 可选: open, in_progress, resolved, escalated, closed")
            return
    orders = query.order_by(DiscrepancyWorkOrder.created_at.desc()).limit(20).all()
    if not orders:
        print("暂无工单数据")
        return
    print(f"{'ID(前8位)':<10} {'责任公司':<10} {'差异金额':>15} {'状态':<12} {'分配给':<10}")
    print("-" * 60)
    for wo in orders:
        print(f"{wo.id[:8]:<10} {wo.responsible_company_code:<10} "
              f"{float(wo.discrepancy_amount or 0):>15,.2f} {wo.status.value:<12} "
              f"{wo.assigned_to or '-':<10}")


def cmd_demo(args):
    """生成演示数据用于测试"""
    from models import Subsidiary, InternalTransaction, init_db, SessionLocal
    from notification import setup_logging
    setup_logging()
    init_db()

    session = SessionLocal()

    subs = [
        Subsidiary(id="sub01", name="华东子公司", code="SUB01",
                    ownership_ratio=Decimal("0.80"), cfo_name="张三",
                    finance_staff="李四,王五", is_active=True),
        Subsidiary(id="sub02", name="华南子公司", code="SUB02",
                    ownership_ratio=Decimal("0.65"), cfo_name="赵六",
                    finance_staff="钱七", is_active=True),
        Subsidiary(id="sub03", name="华北子公司", code="SUB03",
                    ownership_ratio=Decimal("1.00"), cfo_name="孙八",
                    finance_staff="周九", is_active=True),
    ]
    for sub in subs:
        existing = session.query(Subsidiary).filter_by(code=sub.code).first()
        if not existing:
            session.add(sub)
    session.commit()

    import uuid
    base_date = date.today() - timedelta(days=1)
    transactions = []
    for i in range(50):
        buyer_code = subs[i % 3].code
        seller_code = subs[(i + 1) % 3].code
        product_code = f"PROD{i % 10:03d}"
        amount = Decimal(str((i + 1) * 10000))

        transactions.append(InternalTransaction(
            id=str(uuid.uuid4()),
            company_code=buyer_code,
            company_name=subs[i % 3].name,
            counterparty_code=seller_code,
            counterparty_name=subs[(i + 1) % 3].name,
            transaction_date=base_date,
            product_code=product_code,
            product_name=f"商品{i % 10}",
            amount=amount,
            quantity=Decimal(str(i + 1)),
            direction="buy",
            source_system="demo",
        ))
        transactions.append(InternalTransaction(
            id=str(uuid.uuid4()),
            company_code=seller_code,
            company_name=subs[(i + 1) % 3].name,
            counterparty_code=buyer_code,
            counterparty_name=subs[i % 3].name,
            transaction_date=base_date,
            product_code=product_code,
            product_name=f"商品{i % 10}",
            amount=amount,
            quantity=Decimal(str(i + 1)),
            direction="sell",
            source_system="demo",
        ))

    for tx in transactions:
        session.add(tx)
    session.commit()

    print(f"演示数据已生成: {len(subs)} 个子公司, {len(transactions)} 笔交易")
    print("运行以下命令测试:")
    print("  python main.py match-concurrent --date " + base_date.isoformat())
    print("  python main.py list-workorders")


def main():
    parser = argparse.ArgumentParser(
        description="企业级多法人实体内部交易对账与合并报表自动化系统"
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    subparsers.add_parser("init", help="初始化数据库")
    subparsers.add_parser("scheduler", help="启动定时调度器")

    p_fetch = subparsers.add_parser("fetch", help="手动触发交易抓取")
    p_fetch.add_argument("--date", help="目标日期 YYYY-MM-DD")

    p_match = subparsers.add_parser("match", help="手动触发交易匹配")
    p_match.add_argument("--date", help="目标日期 YYYY-MM-DD")

    p_mc = subparsers.add_parser("match-concurrent", help="并发匹配")
    p_mc.add_argument("--date", help="目标日期 YYYY-MM-DD")

    p_import = subparsers.add_parser("import-tb", help="导入试算平衡表")
    p_import.add_argument("--file", required=True, help="文件路径")
    p_import.add_argument("--company", required=True, help="子公司编码")
    p_import.add_argument("--period", required=True, help="期间 YYYY-MM")

    p_validate = subparsers.add_parser("validate", help="校验试算平衡")
    p_validate.add_argument("--period", required=True, help="期间 YYYY-MM")

    p_consol = subparsers.add_parser("consolidate", help="生成合并工作底稿")
    p_consol.add_argument("--period", required=True, help="期间 YYYY-MM")

    p_report = subparsers.add_parser("report", help="生成合并报表")
    p_report.add_argument("--period", required=True, help="期间 YYYY-MM")
    p_report.add_argument("--format", choices=["excel", "pdf", "all"], default="all")

    p_quarterly = subparsers.add_parser("quarterly", help="生成季度报告")
    p_quarterly.add_argument("--year", type=int, required=True)
    p_quarterly.add_argument("--quarter", type=int, choices=[1, 2, 3, 4], required=True)

    subparsers.add_parser("check-timeouts", help="检查超时工单")
    p_late = subparsers.add_parser("check-late", help="检查延迟提交")
    p_late.add_argument("--period", required=True, help="期间 YYYY-MM")
    subparsers.add_parser("check-anomalies", help="检查异常")

    p_elim = subparsers.add_parser("add-elimination", help="手工抵消分录")
    p_elim.add_argument("--period", required=True)
    p_elim.add_argument("--amount", type=float, required=True)
    p_elim.add_argument("--debit-company", default="")
    p_elim.add_argument("--debit-account", default="")
    p_elim.add_argument("--credit-company", default="")
    p_elim.add_argument("--credit-account", default="")
    p_elim.add_argument("--description", default="")
    p_elim.add_argument("--created-by", default="admin")

    p_sub = subparsers.add_parser("add-subsidiary", help="添加子公司")
    p_sub.add_argument("--code", required=True)
    p_sub.add_argument("--name", required=True)
    p_sub.add_argument("--ratio", type=float, required=True, help="持股比例 0-1")
    p_sub.add_argument("--cfo", default="")
    p_sub.add_argument("--staff", default="")

    subparsers.add_parser("list-subsidiaries", help="列出子公司")

    p_wo = subparsers.add_parser("list-workorders", help="查询工单")
    p_wo.add_argument("--status", help="open/in_progress/resolved/escalated/closed")

    subparsers.add_parser("demo", help="生成演示数据")

    args = parser.parse_args()

    commands = {
        "init": cmd_init,
        "scheduler": cmd_scheduler,
        "fetch": cmd_fetch,
        "match": cmd_match,
        "match-concurrent": cmd_match_concurrent,
        "import-tb": cmd_import_tb,
        "validate": cmd_validate,
        "consolidate": cmd_consolidate,
        "report": cmd_report,
        "quarterly": cmd_quarterly,
        "check-timeouts": cmd_check_timeouts,
        "check-late": cmd_check_late,
        "check-anomalies": cmd_check_anomalies,
        "add-elimination": cmd_add_elimination,
        "add-subsidiary": cmd_add_subsidiary,
        "list-subsidiaries": cmd_list_subsidiaries,
        "list-workorders": cmd_list_workorders,
        "demo": cmd_demo,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
