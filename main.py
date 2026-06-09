#!/usr/bin/env python3
"""
企业级多法人实体内部交易对账与合并报表自动化系统 - 主入口CLI

用法:
  python main.py init                          # 初始化数据库
  python main.py scheduler                     # 启动定时调度器
  python main.py fetch --date 2026-06-08       # 手动触发交易抓取
  python main.py match --date 2026-06-08       # 手动触发交易匹配
  python main.py match-concurrent --date 2026-06-08  # 并发匹配
  python main.py import-tb --file data.csv --company SUB01 --period 2026-06
  python main.py validate --period 2026-06     # 校验试算平衡
  python main.py consolidate --period 2026-06  # 生成合并工作底稿和抵消分录
  python main.py report --period 2026-06 --format excel
  python main.py report --period 2026-06 --format pdf
  python main.py quarterly --year 2026 --quarter 2
  python main.py check-timeouts
  python main.py check-late --period 2026-06
  python main.py check-anomalies
  python main.py add-elimination --period 2026-06 --amount 6000000  # 手工抵消分录
  python main.py list-pending-approvals [--period 2026-06]  # 列出待审批抵消分录
  python main.py approve-elimination --entry-id ID --role 子公司CFO --name 张三 [--comment 同意]
  python main.py reject-elimination --entry-id ID --role 子公司CFO --name 张三 --comment 金额有误
  python main.py add-subsidiary --code SUB01 --name "子公司A" --ratio 0.8
  python main.py update-subsidiary-source --code SUB01 --source-type csv --source-url /path/to/data.csv
  python main.py list-subsidiaries
  python main.py list-workorders [--status open]
  python main.py demo                          # 生成演示数据(含差异场景)
"""

import argparse
import sys
import os
import json
import csv
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
    print(f"{'公司编码':<10} {'公司名称':<16} {'导入条数':>8} {'状态':<8} {'耗时':>6} {'说明'}")
    print("-" * 80)
    for code, info in results.items():
        status = info["status"]
        icon = {"成功": "✓", "跳过": "→", "空数据": "○", "失败": "✗"}.get(status, "?")
        elapsed = f"{info.get('elapsed', 0):.1f}s"
        print(f"{code:<10} {info['company_name']:<16} {info['count']:>8} {icon} {status:<6} {elapsed:>6} {info['message']}")


def cmd_match(args):
    from reconciliation import TransactionMatcher
    from notification import setup_logging
    setup_logging()
    target_date = date.fromisoformat(args.date) if args.date else date.today() - timedelta(days=1)
    matcher = TransactionMatcher()
    result = matcher.match_batch(transaction_date=target_date)
    print(f"匹配结果:")
    print(f"  待匹配: {result['total_unmatched']} 笔")
    print(f"  完全匹配: {result['matched']} 笔")
    print(f"  部分匹配: {result['partial']} 笔")
    print(f"  单边流水: {result['single_sided']} 笔")
    print(f"  生成差异工单: {result['discrepancy_created']} 张")


def cmd_match_concurrent(args):
    from reconciliation import TransactionMatcher
    from notification import setup_logging
    setup_logging()
    target_date = date.fromisoformat(args.date) if args.date else date.today() - timedelta(days=1)
    matcher = TransactionMatcher()
    result = matcher.match_concurrent(transaction_date=target_date)
    print(f"并发匹配结果:")
    print(f"  待匹配: {result['total_unmatched']} 笔")
    print(f"  完全匹配: {result['matched']} 笔")
    print(f"  部分匹配: {result['partial']} 笔")
    print(f"  单边流水: {result['single_sided']} 笔")
    print(f"  生成差异工单: {result['discrepancy_created']} 张")


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
    is_draft = getattr(args, "draft", False)

    risks = gen.check_report_risks(args.period)
    if risks:
        print(f"⚠ 报表生成前检查 - 发现 {len(risks)} 个风险项:")
        for r in risks:
            level_icon = "🔴" if r["level"] == "high" else "🟡"
            print(f"  {level_icon} [{r['type']}] {r['description']}")
        print()

        if not is_draft:
            print("存在风险项, 无法生成正式版报表。请先处理上述风险, 或使用 --draft 生成草稿版。")
            return
        print("草稿模式: 忽略风险项, 生成草稿版报表(仅供内部参考)\n")

    if fmt in ("excel", "all"):
        path = gen.export_to_excel(args.period, "all", is_draft=is_draft)
        tag = " (草稿)" if is_draft else ""
        print(f"Excel导出{tag}: {path}")

    if fmt in ("pdf", "all"):
        path = gen.export_to_pdf(args.period, "all", is_draft=is_draft)
        tag = " (草稿)" if is_draft else ""
        print(f"PDF导出{tag}: {path}")


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

    cat_breakdown = result.get("category_breakdown", [])
    if cat_breakdown:
        print(f"  差异原因分类统计:")
        print(f"    {'分类':<14} {'工单数':>6} {'金额合计':>16} {'平均处理时长(h)':>14}")
        for c in cat_breakdown:
            print(f"    {c['category']:<14} {c['count']:>6} {c['total_amount']:>16,.2f} {c['avg_processing_hours']:>14.2f}")

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
        print(f"  ⚠ 金额超500万元, 触发三级审批(子公司CFO → 集团财务总监 → CEO)")
        print(f"  当前待审批: 子公司CFO")
        print(f"  审批命令: python main.py approve-elimination --entry-id {entry.id} --role 子公司CFO --name 审批人姓名")


def cmd_list_pending_approvals(args):
    from consolidation import ConsolidationEngine
    from notification import setup_logging
    setup_logging()
    engine = ConsolidationEngine()
    results = engine.list_pending_approvals(period=args.period)
    if not results:
        print("无待审批的手工抵消分录")
        return
    print(f"{'分录ID(前8位)':<12} {'期间':<10} {'金额':>15} {'当前步骤':<14} {'创建人':<8} {'说明'}")
    print("-" * 85)
    for r in results:
        print(f"{r['entry_id'][:8]:<12} {r['period']:<10} {r['amount']:>15,.2f} "
              f"{r['current_step']:<14} {r.get('created_by', ''):<8} {(r.get('description') or '')[:30]}")
        for a in r["approval_detail"]:
            icon = {"approved": "✓", "pending": "○", "rejected": "✗", "cancelled": "-"}.get(a["status"], "?")
            print(f"  {icon} {a['role']}: {a['status']}", end="")
            if a["approver"]:
                print(f" ({a['approver']})", end="")
            if a["comment"]:
                print(f" - {a['comment']}", end="")
            print()


def cmd_approve_elimination(args):
    from consolidation import ConsolidationEngine
    from notification import setup_logging
    setup_logging()
    engine = ConsolidationEngine()
    success, message = engine.approve_elimination_entry(
        entry_id=args.entry_id,
        approver_role=args.role,
        approver_name=args.name,
        approved=True,
        comment=args.comment or "",
    )
    if success:
        print(f"✓ 审批通过: {message}")
        results = engine.list_pending_approvals()
        for r in results:
            if r["entry_id"] == args.entry_id:
                print(f"  下一步: {r['current_step']}")
                break
        else:
            print(f"  全部审批完成, 该分录将计入合并报表")
    else:
        print(f"✗ 审批失败: {message}")


def cmd_reject_elimination(args):
    from consolidation import ConsolidationEngine
    from notification import setup_logging
    setup_logging()
    engine = ConsolidationEngine()
    success, message = engine.approve_elimination_entry(
        entry_id=args.entry_id,
        approver_role=args.role,
        approver_name=args.name,
        approved=False,
        comment=args.comment or "",
    )
    if success:
        print(f"✗ 已驳回: {message}")
    else:
        print(f"操作失败: {message}")


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
        data_source_type=args.source_type or "csv",
        data_source_url=args.source_url or "",
        is_active=True,
    )
    session.add(sub)
    session.commit()
    print(f"子公司已添加: {sub.name}({sub.code}), 持股比例 {sub.ownership_ratio}")
    if sub.data_source_url:
        print(f"  数据源: {sub.data_source_type} - {sub.data_source_url}")
    else:
        print(f"  数据源: 未配置, 请用 update-subsidiary-source 命令配置")


def cmd_update_subsidiary_source(args):
    from models import Subsidiary, SessionLocal
    from notification import setup_logging
    setup_logging()
    session = SessionLocal()
    sub = session.query(Subsidiary).filter_by(code=args.code).first()
    if not sub:
        print(f"子公司 {args.code} 不存在")
        return
    sub.data_source_type = args.source_type
    sub.data_source_url = args.source_url
    session.commit()
    print(f"子公司 {sub.name}({sub.code}) 数据源已更新: {args.source_type} - {args.source_url}")


def cmd_list_subsidiaries(args):
    from models import Subsidiary, SessionLocal
    from notification import setup_logging
    setup_logging()
    session = SessionLocal()
    subs = session.query(Subsidiary).filter_by(is_active=True).all()
    if not subs:
        print("暂无子公司数据")
        return
    print(f"{'编码':<8} {'名称':<14} {'持股比例':<8} {'CFO':<6} {'数据源类型':<8} {'数据源URL'}")
    print("-" * 80)
    for sub in subs:
        src_url = sub.data_source_url or "(未配置)"
        print(f"{sub.code:<8} {sub.name:<14} {float(sub.ownership_ratio):<8.1%} "
              f"{sub.cfo_name or '-':<6} {sub.data_source_type or 'csv':<8} {src_url}")


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
    print(f"{'ID(前8位)':<10} {'差异类型':<22} {'责任公司':<10} {'差异金额':>15} {'状态':<12} {'分类':<10} {'分配给':<8}")
    print("-" * 95)
    for wo in orders:
        print(f"{wo.id[:8]:<10} {wo.discrepancy_type:<22} {wo.responsible_company_code:<10} "
              f"{float(wo.discrepancy_amount or 0):>15,.2f} {wo.status.value:<12} "
              f"{wo.category or '-':<10} {wo.assigned_to or '-':<8}")


def cmd_show_workorder(args):
    from work_order import WorkOrderManager
    from notification import setup_logging
    setup_logging()
    mgr = WorkOrderManager()
    detail = mgr.get_work_order_detail_dict(args.id)
    if not detail:
        print(f"工单 {args.id} 不存在")
        return
    print(f"=== 工单详情 ===")
    print(f"  ID: {detail['id']}")
    print(f"  差异类型: {detail['discrepancy_type']}")
    print(f"  差异金额: {detail['discrepancy_amount']:,.2f}")
    print(f"  责任公司: {detail['responsible_company_code']}")
    print(f"  分配给: {detail['assigned_to'] or '-'}")
    print(f"  状态: {detail['status']}")
    print(f"  分类: {detail['category'] or '(未分类)'}")
    print(f"  说明: {detail['discrepancy_description']}")
    if detail.get("resolution_note"):
        print(f"  解决说明: {detail['resolution_note']}")
    if detail.get("processing_notes"):
        print(f"  处理记录:")
        for line in detail["processing_notes"].split("\n"):
            print(f"    {line}")
    print(f"  创建时间: {detail['created_at']}")
    if detail.get("resolved_at"):
        print(f"  解决时间: {detail['resolved_at']}")
    if detail.get("related_transactions"):
        print(f"  关联交易:")
        for tx in detail["related_transactions"]:
            print(f"    {tx['direction']} | {tx['company_code']}→{tx['counterparty_code']} "
                  f"| {tx['product_code']} | {tx['amount']:,.2f} | 状态: {tx['match_status']}")


def cmd_update_workorder(args):
    from work_order import WorkOrderManager
    from notification import setup_logging
    setup_logging()
    mgr = WorkOrderManager()
    has_update = any([args.status, args.note, args.category, args.assign])
    if not has_update:
        print("请至少指定一项更新: --status, --note, --category, --assign")
        return
    wo = mgr.update_work_order(
        work_order_id=args.id,
        status=args.status,
        note=args.note,
        category=args.category,
        assigned_to=args.assign,
    )
    if not wo:
        print(f"工单 {args.id} 不存在或状态无效")
        return
    print(f"✓ 工单已更新: ID={wo.id[:8]}")
    print(f"  当前状态: {wo.status.value}")
    if wo.category:
        print(f"  分类: {wo.category}")
    if wo.assigned_to:
        print(f"  分配给: {wo.assigned_to}")


def cmd_list_fetch_batches(args):
    from models import FetchBatch, SessionLocal
    from notification import setup_logging
    setup_logging()
    session = SessionLocal()
    query = session.query(FetchBatch)
    if args.company:
        query = query.filter(FetchBatch.company_code == args.company)
    if args.date:
        fetch_date = date.fromisoformat(args.date)
        query = query.filter(FetchBatch.fetch_date == fetch_date)
    batches = query.order_by(FetchBatch.created_at.desc()).limit(20).all()
    if not batches:
        print("暂无抓取批次记录")
        return
    print(f"{'批次ID(前8位)':<12} {'公司':<10} {'日期':<12} {'状态':<8} {'条数':>6} {'耗时':>6} {'错误信息'}")
    print("-" * 90)
    for b in batches:
        err = (b.error_message or "")[:35]
        print(f"{b.id[:8]:<12} {b.company_code:<10} {str(b.fetch_date):<12} "
              f"{b.status:<8} {b.record_count:>6} {b.duration_seconds:>5.1f}s {err}")


def cmd_list_reports(args):
    from models import ConsolidatedReport, SessionLocal
    from notification import setup_logging
    setup_logging()
    session = SessionLocal()
    query = session.query(ConsolidatedReport)
    if args.period:
        query = query.filter_by(period=args.period)
    reports = query.order_by(ConsolidatedReport.generated_at.desc()).limit(20).all()
    if not reports:
        print("暂无报表记录")
        return
    print(f"{'ID(前8位)':<10} {'期间':<10} {'类型':<6} {'状态':<8} {'PDF':<4} {'Excel':<4} {'风险':>4} {'来源草稿':<10} {'发布人':<8} {'生成时间'}")
    print("-" * 105)
    for r in reports:
        has_pdf = "✓" if r.file_path_pdf else "✗"
        has_excel = "✓" if r.file_path_excel else "✗"
        risk_count = 0
        if r.risk_items:
            try:
                risk_count = len(json.loads(r.risk_items))
            except Exception:
                pass
        gen_time = r.generated_at.strftime("%m-%d %H:%M") if r.generated_at else "-"
        status = r.report_status or ("draft" if r.is_draft else "published")
        status_map = {"draft": "草稿", "published": "已发布", "revoked": "已撤回"}
        status_str = status_map.get(status, status)
        source = r.draft_source_id[:8] if r.draft_source_id else "-"
        publisher = r.published_by or "-"
        print(f"{r.id[:8]:<10} {r.period:<10} {r.report_type:<6} {status_str:<8} "
              f"{has_pdf:<4} {has_excel:<4} {risk_count:>4} {source:<10} {publisher:<8} {gen_time}")


def cmd_publish_report(args):
    from report_generator import ReportGenerator
    from notification import setup_logging
    setup_logging()
    gen = ReportGenerator()
    result = gen.publish_report(args.id, args.published_by)
    if not result:
        print(f"发布失败: 报表 {args.id} 不存在、非草稿状态或仍存在风险项")
        return
    print(f"✓ 正式版已发布")
    print(f"  报表ID: {result.id[:8]}")
    print(f"  期间: {result.period}")
    print(f"  来源草稿: {result.draft_source_id[:8]}")
    print(f"  发布人: {result.published_by}")
    print(f"  发布时间: {result.published_at.strftime('%Y-%m-%d %H:%M')}")
    if result.file_path_excel:
        print(f"  Excel: {result.file_path_excel}")
    if result.file_path_pdf:
        print(f"  PDF: {result.file_path_pdf}")


def cmd_revoke_report(args):
    from report_generator import ReportGenerator
    from notification import setup_logging
    setup_logging()
    gen = ReportGenerator()
    result = gen.revoke_report(args.id, args.revoked_by, args.reason)
    if not result:
        print(f"撤回失败: 报表 {args.id} 不存在或非已发布状态")
        return
    print(f"✓ 正式版已撤回")
    print(f"  报表ID: {result.id[:8]}")
    print(f"  期间: {result.period}")
    print(f"  撤回人: {result.revoked_by}")
    print(f"  撤回时间: {result.revoked_at.strftime('%Y-%m-%d %H:%M')}")
    print(f"  撤回原因: {result.revoke_reason}")


def cmd_audit_report(args):
    from models import (SessionLocal, ConsolidatedReport, FetchBatch,
                         DiscrepancyWorkOrder, WorkOrderStatus, TrialBalance,
                         EliminationEntry, ApprovalStatus, EliminationApproval,
                         OperationLog)
    from notification import setup_logging
    setup_logging()
    session = SessionLocal()
    period = args.period

    print(f"=== {period} 期间报表审计时间线 ===\n")

    print(f"── 1. 数据抓取批次 ──")
    batches = session.query(FetchBatch).filter(
        FetchBatch.fetch_date >= period + "-01",
        FetchBatch.fetch_date <= period + "-31",
    ).order_by(FetchBatch.created_at).all()
    if batches:
        for b in batches:
            print(f"  {b.created_at.strftime('%Y-%m-%d %H:%M') if b.created_at else '-'}  "
                  f"{b.company_code}  {b.status}  {b.record_count}条  "
                  f"{f'错误: {b.error_message[:30]}' if b.error_message else ''}")
    else:
        print("  (无记录)")

    print(f"\n── 2. 差异工单处理 ──")
    work_orders = session.query(DiscrepancyWorkOrder).filter(
        DiscrepancyWorkOrder.discrepancy_description.like(f"%{period}%"),
    ).order_by(DiscrepancyWorkOrder.created_at).all()
    if not work_orders:
        work_orders = session.query(DiscrepancyWorkOrder).filter(
            DiscrepancyWorkOrder.created_at >= period + "-01",
            DiscrepancyWorkOrder.created_at <= period + "-31",
        ).order_by(DiscrepancyWorkOrder.created_at).all()
    if work_orders:
        for wo in work_orders:
            resolved = wo.resolved_at.strftime('%Y-%m-%d %H:%M') if wo.resolved_at else "-"
            print(f"  创建: {wo.created_at.strftime('%Y-%m-%d %H:%M') if wo.created_at else '-'}  "
                  f"类型={wo.discrepancy_type}  金额={float(wo.discrepancy_amount or 0):,.0f}  "
                  f"状态={wo.status.value}  分类={wo.category or '-'}  "
                  f"解决={resolved}")
    else:
        print("  (无记录)")

    print(f"\n── 3. 试算平衡表提交 ──")
    tbs = session.query(TrialBalance).filter_by(period=period).all()
    if tbs:
        seen = set()
        for tb in tbs:
            if tb.company_code not in seen:
                seen.add(tb.company_code)
                print(f"  {tb.company_code}  {tb.account_name}  "
                      f"提交时间={tb.created_at.strftime('%Y-%m-%d %H:%M') if tb.created_at else '-'}")
    else:
        print("  (未提交)")

    print(f"\n── 4. 抵消分录审批 ──")
    elim_entries = session.query(EliminationEntry).filter_by(period=period).all()
    if elim_entries:
        for entry in elim_entries:
            approval_str = entry.approval_status.value if entry.approval_status else "-"
            manual = "(手工)" if entry.is_manual else ""
            print(f"  {entry.created_at.strftime('%Y-%m-%d %H:%M') if entry.created_at else '-'}  "
                  f"金额={float(entry.amount):,.0f}{manual}  审批={approval_str}  "
                  f"描述={entry.description or '-'}")
            for ap in entry.approval_records:
                ap_time = ap.approved_at.strftime('%Y-%m-%d %H:%M') if ap.approved_at else "-"
                print(f"    {ap.approver_role}  {ap.status.value}  {ap.approver_name or '-'}  {ap_time}")
    else:
        print("  (无记录)")

    print(f"\n── 5. 报表发布记录 ──")
    reports = session.query(ConsolidatedReport).filter_by(period=period).order_by(
        ConsolidatedReport.generated_at
    ).all()
    if reports:
        for r in reports:
            status_map = {"draft": "草稿", "published": "已发布", "revoked": "已撤回"}
            status_str = status_map.get(r.report_status or "", r.report_status or "?")
            line = (f"  {r.generated_at.strftime('%Y-%m-%d %H:%M') if r.generated_at else '-'}  "
                    f"ID={r.id[:8]}  {status_str}")
            if r.published_by:
                line += f"  发布人={r.published_by}"
            if r.published_at:
                line += f"  发布时间={r.published_at.strftime('%Y-%m-%d %H:%M')}"
            if r.revoked_by:
                line += f"  撤回人={r.revoked_by}"
            if r.revoke_reason:
                line += f"  原因={r.revoke_reason[:30]}"
            if r.draft_source_id:
                line += f"  来源草稿={r.draft_source_id[:8]}"
            print(line)
    else:
        print("  (无记录)")


def cmd_demo(args):
    """生成演示数据(含差异场景: 金额不一致、商品编码不一致、单边流水)"""
    from models import Subsidiary, InternalTransaction, init_db, SessionLocal
    from notification import setup_logging
    setup_logging()
    init_db()

    session = SessionLocal()

    demo_dir = os.path.join(BASE_DIR, "demo_data")
    os.makedirs(demo_dir, exist_ok=True)

    subs = [
        Subsidiary(id="sub01", name="华东子公司", code="SUB01",
                    ownership_ratio=Decimal("0.80"), cfo_name="张三",
                    finance_staff="李四,王五",
                    data_source_type="csv",
                    data_source_url=os.path.join(demo_dir, "SUB01_{date}.csv"),
                    is_active=True),
        Subsidiary(id="sub02", name="华南子公司", code="SUB02",
                    ownership_ratio=Decimal("0.65"), cfo_name="赵六",
                    finance_staff="钱七",
                    data_source_type="csv",
                    data_source_url=os.path.join(demo_dir, "SUB02_{date}.csv"),
                    is_active=True),
        Subsidiary(id="sub03", name="华北子公司", code="SUB03",
                    ownership_ratio=Decimal("1.00"), cfo_name="孙八",
                    finance_staff="周九",
                    data_source_type="csv",
                    data_source_url=os.path.join(demo_dir, "SUB03_{date}.csv"),
                    is_active=True),
    ]
    for sub in subs:
        existing = session.query(Subsidiary).filter_by(code=sub.code).first()
        if not existing:
            session.add(sub)
    session.commit()

    target_date = date.today() - timedelta(days=1)
    date_str = target_date.isoformat()

    sub01_rows = []
    sub02_rows = []
    sub03_rows = []

    import uuid
    for i in range(20):
        buyer_code = subs[i % 3].code
        seller_code = subs[(i + 1) % 3].code
        product_code = f"PROD{i % 10:03d}"
        amount = (i + 1) * 10000

        if buyer_code == "SUB01":
            sub01_rows.append({"transaction_date": date_str, "counterparty_code": seller_code,
                                "counterparty_name": subs[(i + 1) % 3].name,
                                "product_code": product_code, "product_name": f"商品{i % 10}",
                                "amount": amount, "quantity": i + 1, "direction": "buy",
                                "source_system": "demo"})
        elif buyer_code == "SUB02":
            sub02_rows.append({"transaction_date": date_str, "counterparty_code": seller_code,
                                "counterparty_name": subs[(i + 1) % 3].name,
                                "product_code": product_code, "product_name": f"商品{i % 10}",
                                "amount": amount, "quantity": i + 1, "direction": "buy",
                                "source_system": "demo"})
        else:
            sub03_rows.append({"transaction_date": date_str, "counterparty_code": seller_code,
                                "counterparty_name": subs[(i + 1) % 3].name,
                                "product_code": product_code, "product_name": f"商品{i % 10}",
                                "amount": amount, "quantity": i + 1, "direction": "buy",
                                "source_system": "demo"})

        if seller_code == "SUB01":
            sub01_rows.append({"transaction_date": date_str, "counterparty_code": buyer_code,
                                "counterparty_name": subs[i % 3].name,
                                "product_code": product_code, "product_name": f"商品{i % 10}",
                                "amount": amount, "quantity": i + 1, "direction": "sell",
                                "source_system": "demo"})
        elif seller_code == "SUB02":
            sub02_rows.append({"transaction_date": date_str, "counterparty_code": buyer_code,
                                "counterparty_name": subs[i % 3].name,
                                "product_code": product_code, "product_name": f"商品{i % 10}",
                                "amount": amount, "quantity": i + 1, "direction": "sell",
                                "source_system": "demo"})
        else:
            sub03_rows.append({"transaction_date": date_str, "counterparty_code": buyer_code,
                                "counterparty_name": subs[i % 3].name,
                                "product_code": product_code, "product_name": f"商品{i % 10}",
                                "amount": amount, "quantity": i + 1, "direction": "sell",
                                "source_system": "demo"})

    sub01_rows.append({"transaction_date": date_str, "counterparty_code": "SUB02",
                        "counterparty_name": "华南子公司",
                        "product_code": "PROD999", "product_name": "测试商品-金额差异",
                        "amount": 500000, "quantity": 10, "direction": "buy",
                        "source_system": "demo"})
    sub02_rows.append({"transaction_date": date_str, "counterparty_code": "SUB01",
                        "counterparty_name": "华东子公司",
                        "product_code": "PROD999", "product_name": "测试商品-金额差异",
                        "amount": 450000, "quantity": 9, "direction": "sell",
                        "source_system": "demo"})

    sub01_rows.append({"transaction_date": date_str, "counterparty_code": "SUB03",
                        "counterparty_name": "华北子公司",
                        "product_code": "PROD001", "product_name": "商品A-编码差异买方",
                        "amount": 200000, "quantity": 20, "direction": "buy",
                        "source_system": "demo"})
    sub03_rows.append({"transaction_date": date_str, "counterparty_code": "SUB01",
                        "counterparty_name": "华东子公司",
                        "product_code": "PROD002", "product_name": "商品B-编码差异卖方",
                        "amount": 200000, "quantity": 20, "direction": "sell",
                        "source_system": "demo"})

    sub02_rows.append({"transaction_date": date_str, "counterparty_code": "SUB03",
                        "counterparty_name": "华北子公司",
                        "product_code": "PROD777", "product_name": "单边流水测试",
                        "amount": 800000, "quantity": 80, "direction": "buy",
                        "source_system": "demo"})

    csv_headers = ["transaction_date", "counterparty_code", "counterparty_name",
                    "product_code", "product_name", "amount", "quantity",
                    "direction", "source_system"]

    for code, rows in [("SUB01", sub01_rows), ("SUB02", sub02_rows), ("SUB03", sub03_rows)]:
        filename = f"{code}_{date_str}.csv"
        filepath = os.path.join(demo_dir, filename)
        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=csv_headers)
            writer.writeheader()
            writer.writerows(rows)

    print(f"演示数据已生成:")
    print(f"  3 个子公司 (SUB01/SUB02/SUB03)")
    print(f"  数据源CSV已写入: {demo_dir}/")
    print(f"  包含差异场景: 金额不一致(SUB01↔SUB02 PROD999)、商品编码不一致(SUB01↔SUB03)、单边流水(SUB02→SUB03)")
    print()
    print("运行以下命令测试闭环:")
    print(f"  python main.py fetch --date {date_str}")
    print(f"  python main.py match --date {date_str}")
    print(f"  python main.py list-workorders")


def main():
    parser = argparse.ArgumentParser(
        description="企业级多法人实体内部交易对账与合并报表自动化系统"
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    subparsers.add_parser("init", help="初始化数据库")
    subparsers.add_parser("scheduler", help="启动定时调度器")

    p_fetch = subparsers.add_parser("fetch", help="从各子公司数据源抓取交易流水")
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
    p_report.add_argument("--draft", action="store_true", help="生成草稿版(忽略风险项)")

    p_quarterly = subparsers.add_parser("quarterly", help="生成季度报告")
    p_quarterly.add_argument("--year", type=int, required=True)
    p_quarterly.add_argument("--quarter", type=int, choices=[1, 2, 3, 4], required=True)

    subparsers.add_parser("check-timeouts", help="检查超时工单")
    p_late = subparsers.add_parser("check-late", help="检查延迟提交")
    p_late.add_argument("--period", required=True, help="期间 YYYY-MM")
    subparsers.add_parser("check-anomalies", help="检查异常")

    p_elim = subparsers.add_parser("add-elimination", help="手工抵消分录(超500万触发三级审批)")
    p_elim.add_argument("--period", required=True)
    p_elim.add_argument("--amount", type=float, required=True)
    p_elim.add_argument("--debit-company", default="")
    p_elim.add_argument("--debit-account", default="")
    p_elim.add_argument("--credit-company", default="")
    p_elim.add_argument("--credit-account", default="")
    p_elim.add_argument("--description", default="")
    p_elim.add_argument("--created-by", default="admin")

    p_lpa = subparsers.add_parser("list-pending-approvals", help="列出待审批的手工抵消分录")
    p_lpa.add_argument("--period", default=None, help="筛选期间")

    p_ae = subparsers.add_parser("approve-elimination", help="审批通过抵消分录")
    p_ae.add_argument("--entry-id", required=True, help="分录ID")
    p_ae.add_argument("--role", required=True, choices=["子公司CFO", "集团财务总监", "CEO"])
    p_ae.add_argument("--name", required=True, help="审批人姓名")
    p_ae.add_argument("--comment", default="")

    p_re = subparsers.add_parser("reject-elimination", help="驳回抵消分录")
    p_re.add_argument("--entry-id", required=True, help="分录ID")
    p_re.add_argument("--role", required=True, choices=["子公司CFO", "集团财务总监", "CEO"])
    p_re.add_argument("--name", required=True, help="审批人姓名")
    p_re.add_argument("--comment", required=True, help="驳回原因")

    p_sub = subparsers.add_parser("add-subsidiary", help="添加子公司")
    p_sub.add_argument("--code", required=True)
    p_sub.add_argument("--name", required=True)
    p_sub.add_argument("--ratio", type=float, required=True, help="持股比例 0-1")
    p_sub.add_argument("--cfo", default="")
    p_sub.add_argument("--staff", default="")
    p_sub.add_argument("--source-type", default="csv", choices=["csv", "http"])
    p_sub.add_argument("--source-url", default="")

    p_us = subparsers.add_parser("update-subsidiary-source", help="更新子公司数据源配置")
    p_us.add_argument("--code", required=True, help="子公司编码")
    p_us.add_argument("--source-type", required=True, choices=["csv", "http"])
    p_us.add_argument("--source-url", required=True, help="CSV路径或HTTP地址, 可用{date}占位符")

    subparsers.add_parser("list-subsidiaries", help="列出子公司")

    p_wo = subparsers.add_parser("list-workorders", help="查询差异工单")
    p_wo.add_argument("--status", help="open/in_progress/resolved/escalated/closed")

    subparsers.add_parser("demo", help="生成演示数据(含差异场景和数据源CSV)")

    p_show_wo = subparsers.add_parser("show-workorder", help="查看工单详情")
    p_show_wo.add_argument("--id", required=True, help="工单ID")

    p_update_wo = subparsers.add_parser("update-workorder", help="更新工单状态/分类/备注")
    p_update_wo.add_argument("--id", required=True, help="工单ID")
    p_update_wo.add_argument("--status", choices=["open", "in_progress", "resolved", "escalated", "closed"],
                              help="新状态")
    p_update_wo.add_argument("--note", help="处理备注")
    p_update_wo.add_argument("--category",
                              choices=["数据录入错误", "系统延迟", "价格调整", "退货", "其他"],
                              help="原因分类")
    p_update_wo.add_argument("--assign", help="分配给(人员姓名)")

    p_fb = subparsers.add_parser("list-fetch-batches", help="查询抓取批次记录")
    p_fb.add_argument("--company", help="按公司编码筛选")
    p_fb.add_argument("--date", help="按日期筛选 YYYY-MM-DD")

    p_lr = subparsers.add_parser("list-reports", help="查询历史报表")
    p_lr.add_argument("--period", help="按期间筛选 YYYY-MM")

    p_pub = subparsers.add_parser("publish-report", help="将草稿发布为正式版")
    p_pub.add_argument("--id", required=True, help="草稿报表ID")
    p_pub.add_argument("--published-by", required=True, help="发布人姓名")

    p_revoke = subparsers.add_parser("revoke-report", help="撤回已发布的正式版报表")
    p_revoke.add_argument("--id", required=True, help="报表ID")
    p_revoke.add_argument("--revoked-by", required=True, help="撤回人姓名")
    p_revoke.add_argument("--reason", required=True, help="撤回原因")

    p_audit = subparsers.add_parser("audit-report", help="查看期间报表审计时间线")
    p_audit.add_argument("--period", required=True, help="期间 YYYY-MM")

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
        "list-pending-approvals": cmd_list_pending_approvals,
        "approve-elimination": cmd_approve_elimination,
        "reject-elimination": cmd_reject_elimination,
        "add-subsidiary": cmd_add_subsidiary,
        "update-subsidiary-source": cmd_update_subsidiary_source,
        "list-subsidiaries": cmd_list_subsidiaries,
        "list-workorders": cmd_list_workorders,
        "show-workorder": cmd_show_workorder,
        "update-workorder": cmd_update_workorder,
        "list-fetch-batches": cmd_list_fetch_batches,
        "list-reports": cmd_list_reports,
        "publish-report": cmd_publish_report,
        "revoke-report": cmd_revoke_report,
        "audit-report": cmd_audit_report,
        "demo": cmd_demo,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
