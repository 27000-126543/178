import logging
import signal
import sys
from datetime import datetime, date, timedelta
from typing import Optional

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from models import init_db, SessionLocal
from config import QUARTERLY_REPORT_MONTHS, CONCURRENT_WORKERS
from notification import setup_logging, EscalationService

logger = logging.getLogger(__name__)


class TaskScheduler:
    """定时任务调度器 - 协调所有自动化任务"""

    def __init__(self):
        setup_logging()
        init_db()
        self.scheduler = BlockingScheduler()
        self._setup_signals()

    def _setup_signals(self):
        def shutdown(signum, frame):
            logger.info("收到终止信号, 正在关闭调度器...")
            self.scheduler.shutdown(wait=False)
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

    def setup_daily_tasks(self):
        """每日任务: 抓取交易 + 匹配 + 超时检查"""
        self.scheduler.add_job(
            self._task_daily_fetch,
            CronTrigger(hour=1, minute=0),
            id="daily_fetch",
            name="每日交易流水抓取",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._task_daily_match,
            CronTrigger(hour=2, minute=0),
            id="daily_match",
            name="每日交易匹配",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._task_check_timeouts,
            IntervalTrigger(hours=4),
            id="check_timeouts",
            name="工单超时检查",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._task_check_anomalies,
            IntervalTrigger(hours=6),
            id="check_anomalies",
            name="异常检查",
            replace_existing=True,
        )
        logger.info("每日任务设置完成")

    def setup_monthly_tasks(self):
        """每月任务: 试算平衡表校验 + 合并报表生成 + 催办"""
        self.scheduler.add_job(
            self._task_monthly_validation,
            CronTrigger(day=4, hour=3, minute=0),
            id="monthly_validation",
            name="每月试算平衡表校验",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._task_monthly_consolidation,
            CronTrigger(day=5, hour=3, minute=0),
            id="monthly_consolidation",
            name="每月合并报表生成",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._task_monthly_late_check,
            CronTrigger(day=3, hour=9, minute=0),
            id="monthly_late_check",
            name="每月延迟提交检查",
            replace_existing=True,
        )
        logger.info("每月任务设置完成")

    def setup_quarterly_tasks(self):
        """每季度任务: 生成对账分析报告"""
        for month in QUARTERLY_REPORT_MONTHS:
            self.scheduler.add_job(
                self._task_quarterly_report,
                CronTrigger(month=month, day=6, hour=3, minute=0),
                id=f"quarterly_report_{month}",
                name=f"季度报告生成({month}月)",
                replace_existing=True,
            )
        logger.info("季度任务设置完成")

    def _task_daily_fetch(self):
        logger.info("=== 开始每日交易流水抓取 ===")
        try:
            from reconciliation import TransactionFetcher
            fetcher = TransactionFetcher()
            results = fetcher.daily_fetch_all(date.today() - timedelta(days=1))
            logger.info(f"抓取结果: {results}")
        except Exception as e:
            logger.error(f"每日抓取失败: {e}", exc_info=True)

    def _task_daily_match(self):
        logger.info("=== 开始每日交易匹配 ===")
        try:
            from reconciliation import TransactionMatcher
            matcher = TransactionMatcher()
            result = matcher.match_concurrent(date.today() - timedelta(days=1))
            logger.info(f"匹配结果: {result}")
        except Exception as e:
            logger.error(f"每日匹配失败: {e}", exc_info=True)

    def _task_check_timeouts(self):
        logger.info("=== 开始工单超时检查 ===")
        try:
            svc = EscalationService()
            escalated = svc.check_and_escalate_work_orders()
            logger.info(f"升级工单: {len(escalated)} 个")
        except Exception as e:
            logger.error(f"超时检查失败: {e}", exc_info=True)

    def _task_check_anomalies(self):
        logger.info("=== 开始异常检查 ===")
        try:
            svc = EscalationService()
            long_unresolved = svc.check_long_unresolved(days_threshold=7)
            high_discrepancy = svc.check_high_discrepancy(amount_threshold=1000000)
            logger.info(f"长期未处理: {len(long_unresolved)}, 高差异: {len(high_discrepancy)}")
        except Exception as e:
            logger.error(f"异常检查失败: {e}", exc_info=True)

    def _task_monthly_validation(self):
        period = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        logger.info(f"=== 开始每月试算平衡表校验: {period} ===")
        try:
            from trial_balance import TrialBalanceManager
            mgr = TrialBalanceManager()
            results = mgr.validate_all_companies(period)
            unbalanced = [r for r in results if not r["is_balanced"]]
            logger.info(f"校验完成: {len(results)} 家子公司, 不平衡 {len(unbalanced)} 家")
        except Exception as e:
            logger.error(f"每月校验失败: {e}", exc_info=True)

    def _task_monthly_consolidation(self):
        period = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        logger.info(f"=== 开始每月合并报表生成: {period} ===")
        try:
            from consolidation import ConsolidationEngine
            from report_generator import ReportGenerator

            engine = ConsolidationEngine()
            engine.generate_worksheet(period)
            engine.generate_elimination_entries(period)

            gen = ReportGenerator()
            excel_path = gen.export_to_excel(period, "all")
            pdf_path = gen.export_to_pdf(period, "all")
            logger.info(f"合并报表生成完成: Excel={excel_path}, PDF={pdf_path}")
        except Exception as e:
            logger.error(f"每月合并失败: {e}", exc_info=True)

    def _task_monthly_late_check(self):
        period = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        logger.info(f"=== 开始延迟提交检查: {period} ===")
        try:
            svc = EscalationService()
            late_list = svc.check_and_remind_late_submissions(period)
            logger.info(f"延迟提交: {len(late_list)} 家")
        except Exception as e:
            logger.error(f"延迟检查失败: {e}", exc_info=True)

    def _task_quarterly_report(self):
        now = date.today()
        year = now.year
        quarter = (now.month - 1) // 3
        if quarter == 0:
            quarter = 4
            year -= 1
        logger.info(f"=== 开始季度报告生成: {year}Q{quarter} ===")
        try:
            from quarterly_analyzer import QuarterlyAnalyzer
            analyzer = QuarterlyAnalyzer()
            filepath = analyzer.export_quarterly_report_excel(year, quarter)
            logger.info(f"季度报告生成完成: {filepath}")
        except Exception as e:
            logger.error(f"季度报告生成失败: {e}", exc_info=True)

    def start(self):
        """启动调度器"""
        self.setup_daily_tasks()
        self.setup_monthly_tasks()
        self.setup_quarterly_tasks()

        logger.info("=" * 60)
        logger.info("企业级多法人实体内部交易对账与合并报表自动化系统")
        logger.info("调度器已启动, 等待任务触发...")
        logger.info("=" * 60)

        try:
            self.scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("调度器已停止")
