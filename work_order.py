import uuid
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import and_

from models import (
    SessionLocal, DiscrepancyWorkOrder, WorkOrderStatus,
    EscalationLevel, ApprovalRecord, ApprovalStatus, OperationLog
)
from config import WORK_ORDER_TIMEOUT_HOURS, MANUAL_ADJUSTMENT_THRESHOLD, APPROVAL_FLOW

logger = logging.getLogger(__name__)


class WorkOrderManager:
    """差异工单管理 - 责任分配、升级与审批"""

    def __init__(self, session: Session = None):
        self.session = session or SessionLocal()

    def assign_work_order(self, work_order_id: str, assigned_to: str = None) -> Optional[DiscrepancyWorkOrder]:
        wo = self.session.query(DiscrepancyWorkOrder).filter_by(id=work_order_id).first()
        if not wo:
            logger.error(f"工单 {work_order_id} 不存在")
            return None

        if assigned_to:
            wo.assigned_to = assigned_to
        wo.status = WorkOrderStatus.IN_PROGRESS
        wo.updated_at = datetime.utcnow()
        self.session.commit()

        self._log_operation("assign_work_order", work_order_id, f"分配给 {wo.assigned_to}")
        logger.info(f"工单 {work_order_id} 已分配给 {wo.assigned_to}")
        return wo

    def resolve_work_order(self, work_order_id: str, resolution_note: str,
                           resolved_by: str = "") -> Optional[DiscrepancyWorkOrder]:
        wo = self.session.query(DiscrepancyWorkOrder).filter_by(id=work_order_id).first()
        if not wo:
            return None

        wo.status = WorkOrderStatus.RESOLVED
        wo.resolved_at = datetime.utcnow()
        wo.resolution_note = resolution_note
        wo.updated_at = datetime.utcnow()

        if wo.discrepancy_amount and wo.discrepancy_amount >= Decimal(str(MANUAL_ADJUSTMENT_THRESHOLD)):
            wo.status = WorkOrderStatus.ESCALATED
            self._initiate_three_level_approval(wo, resolved_by)

        self.session.commit()
        self._log_operation("resolve_work_order", work_order_id,
                            f"解决: {resolution_note[:100]}", resolved_by)
        return wo

    def _initiate_three_level_approval(self, wo: DiscrepancyWorkOrder, initiator: str = ""):
        """三级审批流程: 子公司CFO → 集团财务总监 → CEO"""
        for i, role in enumerate(APPROVAL_FLOW):
            approval = ApprovalRecord(
                id=str(uuid.uuid4()),
                work_order_id=wo.id,
                approval_type="manual_adjustment",
                approver_role=role,
                status=ApprovalStatus.PENDING if i == 0 else ApprovalStatus.PENDING,
            )
            self.session.add(approval)
        logger.info(f"工单 {wo.id} 差异金额 {wo.discrepancy_amount} 超阈值, 启动三级审批")

    def approve_at_level(self, work_order_id: str, approver_role: str,
                         approver_name: str, approved: bool,
                         comment: str = "") -> bool:
        wo = self.session.query(DiscrepancyWorkOrder).filter_by(id=work_order_id).first()
        if not wo:
            return False

        pending = self.session.query(ApprovalRecord).filter(
            ApprovalRecord.work_order_id == work_order_id,
            ApprovalRecord.approver_role == approver_role,
            ApprovalRecord.status == ApprovalStatus.PENDING
        ).first()

        if not pending:
            logger.warning(f"工单 {work_order_id} 无待审批记录: {approver_role}")
            return False

        if approved:
            pending.status = ApprovalStatus.APPROVED
            pending.approver_name = approver_name
            pending.approved_at = datetime.utcnow()
            pending.comment = comment

            next_idx = APPROVAL_FLOW.index(approver_role) + 1
            if next_idx >= len(APPROVAL_FLOW):
                wo.status = WorkOrderStatus.RESOLVED
                wo.resolved_at = datetime.utcnow()
                logger.info(f"工单 {work_order_id} 三级审批全部通过")
            else:
                logger.info(f"工单 {work_order_id} {approver_role} 审批通过, 等待 {APPROVAL_FLOW[next_idx]}")
        else:
            pending.status = ApprovalStatus.REJECTED
            pending.approver_name = approver_name
            pending.approved_at = datetime.utcnow()
            pending.comment = comment
            wo.status = WorkOrderStatus.OPEN
            logger.info(f"工单 {work_order_id} {approver_role} 审批驳回")

        self.session.commit()
        self._log_operation("approve", work_order_id,
                            f"{approver_role} {'通过' if approved else '驳回'}: {comment}",
                            approver_name)
        return True

    def check_timeout_and_escalate(self) -> List[str]:
        """检查超48小时未解决的工单并升级"""
        timeout_threshold = datetime.utcnow() - timedelta(hours=WORK_ORDER_TIMEOUT_HOURS)
        timeout_orders = self.session.query(DiscrepancyWorkOrder).filter(
            DiscrepancyWorkOrder.status.in_([WorkOrderStatus.OPEN, WorkOrderStatus.IN_PROGRESS]),
            DiscrepancyWorkOrder.created_at <= timeout_threshold,
        ).all()

        escalated_ids = []
        for wo in timeout_orders:
            if wo.escalation_level is None:
                wo.escalation_level = EscalationLevel.LEVEL_2
                wo.status = WorkOrderStatus.ESCALATED
                wo.escalated_at = datetime.utcnow()
                escalated_ids.append(wo.id)
                logger.warning(f"工单 {wo.id} 超时未处理, 升级至集团财务总监")
            elif wo.escalation_level == EscalationLevel.LEVEL_2:
                elapsed_since_escalation = datetime.utcnow() - (wo.escalated_at or wo.created_at)
                if elapsed_since_escalation > timedelta(hours=WORK_ORDER_TIMEOUT_HOURS):
                    wo.escalation_level = EscalationLevel.LEVEL_3
                    wo.escalated_at = datetime.utcnow()
                    escalated_ids.append(wo.id)
                    logger.warning(f"工单 {wo.id} 二级升级后仍超时, 升级至CEO")

        self.session.commit()
        if escalated_ids:
            self._log_operation("escalate_batch", None,
                                f"批量升级工单 {len(escalated_ids)} 个", is_anomaly=True)
        return escalated_ids

    def get_work_orders(self, status: WorkOrderStatus = None,
                        company_code: str = None) -> List[DiscrepancyWorkOrder]:
        query = self.session.query(DiscrepancyWorkOrder)
        if status:
            query = query.filter_by(status=status)
        if company_code:
            query = query.filter_by(responsible_company_code=company_code)
        return query.order_by(DiscrepancyWorkOrder.created_at.desc()).all()

    def get_work_order_detail(self, work_order_id: str) -> Optional[DiscrepancyWorkOrder]:
        return self.session.query(DiscrepancyWorkOrder).filter_by(id=work_order_id).first()

    def close_work_order(self, work_order_id: str, closed_by: str = "") -> bool:
        wo = self.session.query(DiscrepancyWorkOrder).filter_by(id=work_order_id).first()
        if not wo:
            return False
        wo.status = WorkOrderStatus.CLOSED
        wo.updated_at = datetime.utcnow()
        self.session.commit()
        self._log_operation("close_work_order", work_order_id, "关闭工单", closed_by)
        return True

    def _log_operation(self, op_type: str, target_id: str, detail: str,
                       operator: str = "system", is_anomaly: bool = False):
        log = OperationLog(
            id=str(uuid.uuid4()),
            operation_type=op_type,
            operator=operator,
            target_type="work_order",
            target_id=target_id or "",
            detail=detail,
            is_anomaly=is_anomaly,
        )
        self.session.add(log)
        self.session.commit()
