import uuid
import json
import logging
import os
from datetime import datetime
from decimal import Decimal
from typing import List, Dict, Optional

from sqlalchemy.orm import Session
from sqlalchemy import and_

from models import (
    SessionLocal, OperationLog, DiscrepancyWorkOrder,
    WorkOrderStatus, EscalationLevel, ReportSubmission, ReportStatus,
)
from config import (
    NOTIFICATION_WEBHOOK_URL, NOTIFICATION_ENABLED, LOG_FILE,
    LOG_MAX_BYTES, LOG_BACKUP_COUNT, WORK_ORDER_TIMEOUT_HOURS,
    REPORT_SUBMISSION_DELAY_DAYS, DIFFERENCE_TOLERANCE,
)

logger = logging.getLogger(__name__)


def setup_logging():
    log_dir = os.path.dirname(LOG_FILE)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if not root_logger.handlers:
        from logging.handlers import RotatingFileHandler
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        ))
        root_logger.addHandler(file_handler)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s"
        ))
        root_logger.addHandler(console_handler)


class OperationLogger:
    """操作日志记录器"""

    def __init__(self, session: Session = None):
        self.session = session or SessionLocal()

    def log(self, operation_type: str, operator: str = "system",
            target_type: str = None, target_id: str = None,
            detail: str = "", is_anomaly: bool = False):
        record = OperationLog(
            id=str(uuid.uuid4()),
            operation_type=operation_type,
            operator=operator,
            target_type=target_type or "",
            target_id=target_id or "",
            detail=detail,
            is_anomaly=is_anomaly,
        )
        self.session.add(record)
        self.session.commit()

        if is_anomaly:
            self._trigger_anomaly_notification(operation_type, detail, target_id)

        logger.info(f"[LOG] {operation_type} by {operator}: {detail[:80]}")

    def query_logs(self, operation_type: str = None, is_anomaly: bool = None,
                   start_time: datetime = None, end_time: datetime = None,
                   limit: int = 100) -> List[OperationLog]:
        query = self.session.query(OperationLog)
        if operation_type:
            query = query.filter_by(operation_type=operation_type)
        if is_anomaly is not None:
            query = query.filter_by(is_anomaly=is_anomaly)
        if start_time:
            query = query.filter(OperationLog.created_at >= start_time)
        if end_time:
            query = query.filter(OperationLog.created_at <= end_time)
        return query.order_by(OperationLog.created_at.desc()).limit(limit).all()

    def _trigger_anomaly_notification(self, op_type: str, detail: str, target_id: str):
        notifier = NotificationService()
        notifier.send_anomaly_alert(
            title=f"异常告警: {op_type}",
            content=detail,
            target_id=target_id,
        )


class NotificationService:
    """企业群推送通知服务"""

    def __init__(self):
        self.enabled = NOTIFICATION_ENABLED
        self.webhook_url = NOTIFICATION_WEBHOOK_URL

    def send_anomaly_alert(self, title: str, content: str, target_id: str = ""):
        message = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"## {title}\n> {content}\n\n> 关联ID: {target_id}\n> 时间: {datetime.utcnow().isoformat()}"
            },
        }
        self._do_send(message, title)

    def send_escalation_notice(self, work_order_id: str, escalation_level: str,
                                detail: str):
        message = {
            "msgtype": "markdown",
            "markdown": {
                "content": (
                    f"## 工单升级通知\n"
                    f"> 工单ID: {work_order_id}\n"
                    f"> 升级至: {escalation_level}\n"
                    f"> 详情: {detail}\n"
                    f"> 时间: {datetime.utcnow().isoformat()}"
                )
            },
        }
        self._do_send(message, "工单升级通知")

    def send_reminder(self, company_code: str, company_name: str,
                      reminder_type: str, detail: str):
        message = {
            "msgtype": "markdown",
            "markdown": {
                "content": (
                    f"## {reminder_type}\n"
                    f"> 子公司: {company_name}({company_code})\n"
                    f"> 详情: {detail}\n"
                    f"> 时间: {datetime.utcnow().isoformat()}"
                )
            },
        }
        self._do_send(message, reminder_type)

    def send_approval_request(self, entry_id: str, approver_role: str,
                               amount: str, description: str):
        message = {
            "msgtype": "markdown",
            "markdown": {
                "content": (
                    f"## 审批请求\n"
                    f"> 抵消分录ID: {entry_id}\n"
                    f"> 审批角色: {approver_role}\n"
                    f"> 金额: {amount}\n"
                    f"> 说明: {description}\n"
                    f"> 时间: {datetime.utcnow().isoformat()}"
                )
            },
        }
        self._do_send(message, "审批请求")

    def _do_send(self, message: Dict, title: str):
        if not self.enabled or not self.webhook_url:
            logger.info(f"[通知-模拟] {title}: {json.dumps(message, ensure_ascii=False)[:200]}")
            return

        try:
            import urllib.request
            data = json.dumps(message, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                self.webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = resp.read().decode("utf-8")
                logger.info(f"通知发送成功: {title}, 响应: {result[:100]}")
        except Exception as e:
            logger.error(f"通知发送失败: {title}, 错误: {e}")


class EscalationService:
    """催办与升级服务"""

    def __init__(self, session: Session = None):
        self.session = session or SessionLocal()
        self.notifier = NotificationService()

    def check_and_escalate_work_orders(self) -> List[str]:
        """超48小时未解决工单升级"""
        from datetime import timedelta
        timeout = datetime.utcnow() - timedelta(hours=WORK_ORDER_TIMEOUT_HOURS)

        open_orders = self.session.query(DiscrepancyWorkOrder).filter(
            DiscrepancyWorkOrder.status.in_([WorkOrderStatus.OPEN, WorkOrderStatus.IN_PROGRESS]),
            DiscrepancyWorkOrder.created_at <= timeout,
        ).all()

        escalated = []
        for wo in open_orders:
            if wo.escalation_level is None:
                wo.escalation_level = EscalationLevel.LEVEL_2
                wo.escalated_at = datetime.utcnow()
                wo.status = WorkOrderStatus.ESCALATED
                escalated.append(wo.id)

                self.notifier.send_escalation_notice(
                    wo.id, "集团财务总监",
                    f"工单超{WORK_ORDER_TIMEOUT_HOURS}小时未处理, 从子公司级升级至集团财务总监"
                )
            elif wo.escalation_level == EscalationLevel.LEVEL_2:
                second_timeout = datetime.utcnow() - timedelta(hours=WORK_ORDER_TIMEOUT_HOURS)
                if wo.escalated_at and wo.escalated_at <= second_timeout:
                    wo.escalation_level = EscalationLevel.LEVEL_3
                    wo.escalated_at = datetime.utcnow()

                    self.notifier.send_escalation_notice(
                        wo.id, "CEO",
                        f"工单二级升级后仍超时, 升级至CEO"
                    )

        self.session.commit()
        return escalated

    def check_and_remind_late_submissions(self, period: str) -> List[Dict]:
        """超2天未提交报表催办"""
        from datetime import date, timedelta
        try:
            year, month = int(period[:4]), int(period[5:7])
            if month == 12:
                next_first = date(year + 1, 1, 1)
            else:
                next_first = date(year, month + 1, 1)
            deadline = datetime.combine(
                next_first + timedelta(days=REPORT_SUBMISSION_DELAY_DAYS),
                datetime.min.time(),
            )
        except Exception:
            deadline = datetime.utcnow()

        from models import Subsidiary
        subsidiaries = self.session.query(Subsidiary).filter_by(is_active=True).all()

        late_list = []
        for sub in subsidiaries:
            submission = self.session.query(ReportSubmission).filter(
                ReportSubmission.company_code == sub.code,
                ReportSubmission.period == period,
            ).first()

            already_submitted = submission and submission.status == ReportStatus.SUBMITTED
            if already_submitted:
                continue

            if datetime.utcnow() > deadline:
                if submission:
                    submission.is_late = True
                    submission.reminder_count += 1
                    submission.last_reminder_at = datetime.utcnow()
                else:
                    submission = ReportSubmission(
                        id=str(uuid.uuid4()),
                        company_code=sub.code,
                        period=period,
                        report_type="trial_balance",
                        status=ReportStatus.PENDING,
                        is_late=True,
                        reminder_count=1,
                        last_reminder_at=datetime.utcnow(),
                    )
                    self.session.add(submission)

                self.notifier.send_reminder(
                    sub.code, sub.name,
                    "报表提交催办",
                    f"截止日期已过{REPORT_SUBMISSION_DELAY_DAYS}天, 请尽快提交{period}期试算平衡表"
                )

                should_escalate = (
                    datetime.utcnow() - deadline > timedelta(days=REPORT_SUBMISSION_DELAY_DAYS * 2)
                )
                if should_escalate:
                    self.notifier.send_reminder(
                        sub.code, sub.name,
                        "报表延迟升级通知",
                        f"子公司{sub.name}报表严重延迟, 升级至集团财务总监"
                    )

                late_list.append({
                    "company_code": sub.code,
                    "company_name": sub.name,
                    "reminder_count": submission.reminder_count if submission else 0,
                    "should_escalate": should_escalate,
                })

        self.session.commit()
        return late_list

    def check_long_unresolved(self, days_threshold: int = 7) -> List[Dict]:
        """检查长期未处理的异常"""
        from datetime import timedelta
        threshold = datetime.utcnow() - timedelta(days=days_threshold)

        long_unresolved = self.session.query(DiscrepancyWorkOrder).filter(
            DiscrepancyWorkOrder.status.in_([WorkOrderStatus.OPEN, WorkOrderStatus.IN_PROGRESS]),
            DiscrepancyWorkOrder.created_at <= threshold,
        ).all()

        results = []
        for wo in long_unresolved:
            days_open = (datetime.utcnow() - wo.created_at).days
            self.notifier.send_anomaly_alert(
                title="长期未处理工单告警",
                content=f"工单{wo.id}已超{days_open}天未处理, 差异金额{wo.discrepancy_amount}",
                target_id=wo.id,
            )
            results.append({
                "work_order_id": wo.id,
                "days_open": days_open,
                "discrepancy_amount": float(wo.discrepancy_amount) if wo.discrepancy_amount else 0,
                "responsible_company": wo.responsible_company_code,
            })

        return results

    def check_high_discrepancy(self, amount_threshold: float = 1000000) -> List[Dict]:
        """检查对账差异超阈值的异常"""
        high_discrepancies = self.session.query(DiscrepancyWorkOrder).filter(
            DiscrepancyWorkOrder.discrepancy_amount >= Decimal(str(amount_threshold)),
            DiscrepancyWorkOrder.status.in_([WorkOrderStatus.OPEN, WorkOrderStatus.IN_PROGRESS]),
        ).all()

        results = []
        for wo in high_discrepancies:
            self.notifier.send_anomaly_alert(
                title="对账差异超阈值告警",
                content=f"工单{wo.id}差异金额{wo.discrepancy_amount}超阈值{amount_threshold}",
                target_id=wo.id,
            )
            results.append({
                "work_order_id": wo.id,
                "discrepancy_amount": float(wo.discrepancy_amount),
                "threshold": amount_threshold,
            })

        return results
