import uuid
import logging
from datetime import datetime
from decimal import Decimal
from typing import List, Dict, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import func

from models import (
    SessionLocal, Subsidiary, TrialBalance, InternalTransaction,
    MatchStatus, ConsolidationWorksheet, EliminationEntry,
    EliminationApproval, ApprovalStatus, OperationLog
)
from config import MANUAL_ADJUSTMENT_THRESHOLD, APPROVAL_FLOW

logger = logging.getLogger(__name__)


class ConsolidationEngine:
    """合并报表引擎 - 少数股东权益计算、合并工作底稿、抵消分录"""

    def __init__(self, session: Session = None):
        self.session = session or SessionLocal()

    def calculate_minority_interest(self, period: str) -> List[Dict]:
        """根据持股比例自动计算少数股东权益和损益"""
        subsidiaries = self.session.query(Subsidiary).filter_by(is_active=True).all()
        results = []

        for sub in subsidiaries:
            ownership = float(sub.ownership_ratio)
            minority_ratio = 1.0 - ownership
            if minority_ratio <= 0:
                continue

            net_profit = Decimal("0")
            equity = Decimal("0")
            profit_accounts = self.session.query(TrialBalance).filter(
                TrialBalance.company_code == sub.code,
                TrialBalance.period == period,
                TrialBalance.account_code.startswith("5"),
            ).all()
            for acc in profit_accounts:
                if acc.account_type in ("revenue", "income"):
                    net_profit += acc.credit_amount - acc.debit_amount
                elif acc.account_type in ("expense", "cost"):
                    net_profit -= acc.debit_amount - acc.credit_amount

            equity_accounts = self.session.query(TrialBalance).filter(
                TrialBalance.company_code == sub.code,
                TrialBalance.period == period,
                TrialBalance.account_code.startswith("4"),
            ).all()
            for acc in equity_accounts:
                equity += acc.closing_balance

            minority_equity = equity * Decimal(str(minority_ratio))
            minority_profit = net_profit * Decimal(str(minority_ratio))

            result = {
                "company_code": sub.code,
                "company_name": sub.name,
                "ownership_ratio": ownership,
                "minority_ratio": minority_ratio,
                "total_equity": float(equity),
                "minority_equity": float(minority_equity),
                "net_profit": float(net_profit),
                "minority_profit": float(minority_profit),
            }
            results.append(result)
            logger.info(f"子公司 {sub.name} 少数股东权益: {minority_equity}, 少数股东损益: {minority_profit}")

        return results

    def generate_worksheet(self, period: str) -> Dict:
        """生成合并工作底稿"""
        subsidiaries = self.session.query(Subsidiary).filter_by(is_active=True).all()
        self.session.query(ConsolidationWorksheet).filter_by(period=period).delete()
        self.session.commit()

        minority_data = self.calculate_minority_interest(period)
        minority_map = {m["company_code"]: m for m in minority_data}

        total_original = Decimal("0")
        total_adjustment = Decimal("0")
        total_consolidated = Decimal("0")
        total_minority = Decimal("0")
        row_count = 0

        for sub in subsidiaries:
            accounts = self.session.query(TrialBalance).filter(
                TrialBalance.company_code == sub.code,
                TrialBalance.period == period,
            ).all()

            for acc in accounts:
                adjustment = Decimal("0")
                minority = Decimal("0")

                if acc.account_code.startswith("4") and sub.code in minority_map:
                    m = minority_map[sub.code]
                    minority = Decimal(str(m["minority_equity"]))
                    if acc.account_code.startswith("40"):
                        adjustment = -minority

                if acc.account_code.startswith("5") and sub.code in minority_map:
                    m = minority_map[sub.code]
                    minority = Decimal(str(m["minority_profit"]))
                    if acc.account_code.startswith("54"):
                        adjustment = -minority

                consolidated = acc.closing_balance + adjustment

                ws = ConsolidationWorksheet(
                    id=str(uuid.uuid4()),
                    period=period,
                    company_code=sub.code,
                    account_code=acc.account_code,
                    account_name=acc.account_name,
                    original_amount=acc.closing_balance,
                    adjustment_amount=adjustment,
                    consolidated_amount=consolidated,
                    minority_interest_amount=minority,
                )
                self.session.add(ws)

                total_original += acc.closing_balance
                total_adjustment += adjustment
                total_consolidated += consolidated
                total_minority += minority
                row_count += 1

        self.session.commit()
        logger.info(f"合并工作底稿生成完成: {row_count} 行")
        return {
            "period": period,
            "total_rows": row_count,
            "total_original": float(total_original),
            "total_adjustment": float(total_adjustment),
            "total_consolidated": float(total_consolidated),
            "total_minority_interest": float(total_minority),
        }

    def generate_elimination_entries(self, period: str) -> List[Dict]:
        """自动生成抵消分录"""
        entries_created = []

        internal_revenue = self._eliminate_internal_revenue(period)
        entries_created.extend(internal_revenue)

        internal_payable = self._eliminate_internal_payable(period)
        entries_created.extend(internal_payable)

        internal_inventory = self._eliminate_internal_inventory_profit(period)
        entries_created.extend(internal_inventory)

        logger.info(f"自动生成抵消分录 {len(entries_created)} 条")
        return entries_created

    def _eliminate_internal_revenue(self, period: str) -> List[Dict]:
        """抵消内部收入与成本"""
        matched = self.session.query(InternalTransaction).filter(
            InternalTransaction.match_status == MatchStatus.MATCHED,
        ).all()

        entries = []
        revenue_pairs: Dict[str, Decimal] = {}
        for tx in matched:
            key = f"{tx.company_code}|{tx.counterparty_code}|{tx.product_code}"
            if tx.direction == "sell":
                revenue_pairs.setdefault(key, Decimal("0"))
                revenue_pairs[key] += tx.amount

        for key, amount in revenue_pairs.items():
            parts = key.split("|")
            seller_code = parts[0]
            buyer_code = parts[1]
            product_code = parts[2]

            entry_sell = EliminationEntry(
                id=str(uuid.uuid4()),
                period=period,
                entry_type="internal_revenue",
                debit_company=seller_code,
                debit_account="6001",
                credit_company=seller_code,
                credit_account="6401",
                amount=amount,
                description=f"抵消内部销售收入: {seller_code}→{buyer_code}, 商品{product_code}",
            )
            self.session.add(entry_sell)

            entry_cost = EliminationEntry(
                id=str(uuid.uuid4()),
                period=period,
                entry_type="internal_cost",
                debit_company=buyer_code,
                debit_account="6401",
                credit_company=buyer_code,
                credit_account="6001",
                amount=amount,
                description=f"抵消内部采购成本: {buyer_code}←{seller_code}, 商品{product_code}",
            )
            self.session.add(entry_cost)

            entries.append({"type": "internal_revenue", "amount": float(amount),
                            "seller": seller_code, "buyer": buyer_code})

        self.session.commit()
        return entries

    def _eliminate_internal_payable(self, period: str) -> List[Dict]:
        """抵消内部应收应付"""
        matched = self.session.query(InternalTransaction).filter(
            InternalTransaction.match_status == MatchStatus.MATCHED,
        ).all()

        entries = []
        balance_map: Dict[str, Decimal] = {}
        for tx in matched:
            if tx.direction == "sell":
                key = f"{tx.company_code}|{tx.counterparty_code}"
                balance_map.setdefault(key, Decimal("0"))
                balance_map[key] += tx.amount

        for key, amount in balance_map.items():
            parts = key.split("|")
            seller_code = parts[0]
            buyer_code = parts[1]

            entry_ap = EliminationEntry(
                id=str(uuid.uuid4()),
                period=period,
                entry_type="internal_payable",
                debit_company=buyer_code,
                debit_account="2202",
                credit_company=seller_code,
                credit_account="1122",
                amount=amount,
                description=f"抵消内部应收应付: {seller_code}应收/{buyer_code}应付",
            )
            self.session.add(entry_ap)
            entries.append({"type": "internal_payable", "amount": float(amount),
                            "seller": seller_code, "buyer": buyer_code})

        self.session.commit()
        return entries

    def _eliminate_internal_inventory_profit(self, period: str) -> List[Dict]:
        """抵消内部存货未实现利润"""
        entries = []
        unrealized_rate = Decimal("0.10")
        matched = self.session.query(InternalTransaction).filter(
            InternalTransaction.match_status == MatchStatus.MATCHED,
        ).all()

        inventory_pairs: Dict[str, Decimal] = {}
        for tx in matched:
            if tx.direction == "sell":
                key = f"{tx.counterparty_code}|{tx.product_code}"
                inventory_pairs.setdefault(key, Decimal("0"))
                inventory_pairs[key] += tx.amount

        for key, cost_amount in inventory_pairs.items():
            parts = key.split("|")
            buyer_code = parts[0]
            product_code = parts[1]
            unrealized = cost_amount * unrealized_rate

            entry = EliminationEntry(
                id=str(uuid.uuid4()),
                period=period,
                entry_type="inventory_unrealized_profit",
                debit_company=buyer_code,
                debit_account="6401",
                credit_company=buyer_code,
                credit_account="1405",
                amount=unrealized,
                description=f"抵消内部存货未实现利润: {buyer_code}, 商品{product_code}",
            )
            self.session.add(entry)
            entries.append({"type": "inventory_profit", "amount": float(unrealized),
                            "buyer": buyer_code})

        self.session.commit()
        return entries

    def add_manual_elimination_entry(self, period: str, entry_data: Dict,
                                     created_by: str = "") -> EliminationEntry:
        """手工调整抵消分录, 超500万元触发三级审批"""
        amount = Decimal(str(entry_data.get("amount", 0)))
        requires_approval = amount >= Decimal(str(MANUAL_ADJUSTMENT_THRESHOLD))

        entry = EliminationEntry(
            id=str(uuid.uuid4()),
            period=period,
            entry_type=entry_data.get("entry_type", "manual_adjustment"),
            debit_company=entry_data.get("debit_company", ""),
            debit_account=entry_data.get("debit_account", ""),
            credit_company=entry_data.get("credit_company", ""),
            credit_account=entry_data.get("credit_account", ""),
            amount=amount,
            description=entry_data.get("description", "手工调整抵消分录"),
            is_manual=True,
            requires_approval=requires_approval,
            approval_status=ApprovalStatus.PENDING if requires_approval else ApprovalStatus.APPROVED,
            created_by=created_by,
        )
        self.session.add(entry)

        if requires_approval:
            for role in APPROVAL_FLOW:
                approval = EliminationApproval(
                    id=str(uuid.uuid4()),
                    entry_id=entry.id,
                    approver_role=role,
                    status=ApprovalStatus.PENDING,
                )
                self.session.add(approval)
            logger.warning(f"手工调整金额 {amount} 超阈值 {MANUAL_ADJUSTMENT_THRESHOLD}, 触发三级审批")

        self.session.commit()

        self._log_operation("add_manual_elimination", entry.id,
                            f"手工抵消分录金额 {amount}, 审批{'需要' if requires_approval else '不需要'}",
                            created_by)
        return entry

    def approve_elimination_entry(self, entry_id: str, approver_role: str,
                                  approver_name: str, approved: bool,
                                  comment: str = "") -> Tuple[bool, str]:
        entry = self.session.query(EliminationEntry).filter_by(id=entry_id).first()
        if not entry:
            return False, "抵消分录不存在"

        if not entry.requires_approval:
            return False, "该分录无需审批"

        if entry.approval_status == ApprovalStatus.APPROVED:
            return False, "该分录已审批通过"
        if entry.approval_status == ApprovalStatus.REJECTED:
            return False, "该分录已被驳回"

        role_idx = APPROVAL_FLOW.index(approver_role) if approver_role in APPROVAL_FLOW else -1
        if role_idx < 0:
            return False, f"无效审批角色: {approver_role}"

        for prev_idx in range(role_idx):
            prev_role = APPROVAL_FLOW[prev_idx]
            prev_approval = self.session.query(EliminationApproval).filter(
                EliminationApproval.entry_id == entry_id,
                EliminationApproval.approver_role == prev_role,
            ).first()
            if not prev_approval or prev_approval.status != ApprovalStatus.APPROVED:
                return False, f"前置审批未通过: {prev_role}，请先完成该角色审批"

        current_approval = self.session.query(EliminationApproval).filter(
            EliminationApproval.entry_id == entry_id,
            EliminationApproval.approver_role == approver_role,
        ).first()

        if not current_approval:
            return False, f"未找到 {approver_role} 的审批记录"

        if current_approval.status != ApprovalStatus.PENDING:
            return False, f"{approver_role} 已审批(状态: {current_approval.status.value})"

        if approved:
            current_approval.status = ApprovalStatus.APPROVED
            current_approval.approver_name = approver_name
            current_approval.approved_at = datetime.utcnow()
            current_approval.comment = comment

            next_idx = role_idx + 1
            if next_idx >= len(APPROVAL_FLOW):
                entry.approval_status = ApprovalStatus.APPROVED
                logger.info(f"抵消分录 {entry_id} 三级审批全部通过")
            else:
                logger.info(f"抵消分录 {entry_id} {approver_role} 通过, 等待 {APPROVAL_FLOW[next_idx]}")
        else:
            current_approval.status = ApprovalStatus.REJECTED
            current_approval.approver_name = approver_name
            current_approval.approved_at = datetime.utcnow()
            current_approval.comment = comment
            entry.approval_status = ApprovalStatus.REJECTED
            for later_idx in range(role_idx + 1, len(APPROVAL_FLOW)):
                later_approval = self.session.query(EliminationApproval).filter(
                    EliminationApproval.entry_id == entry_id,
                    EliminationApproval.approver_role == APPROVAL_FLOW[later_idx],
                ).first()
                if later_approval and later_approval.status == ApprovalStatus.PENDING:
                    later_approval.status = ApprovalStatus.CANCELLED
            logger.info(f"抵消分录 {entry_id} {approver_role} 驳回")

        self.session.commit()
        return True, "审批成功" if approved else "已驳回"

    def list_pending_approvals(self, period: str = None) -> List[Dict]:
        query = self.session.query(EliminationEntry).filter(
            EliminationEntry.is_manual == True,
            EliminationEntry.requires_approval == True,
            EliminationEntry.approval_status == ApprovalStatus.PENDING,
        )
        if period:
            query = query.filter_by(period=period)

        entries = query.all()
        results = []
        for entry in entries:
            approvals = self.session.query(EliminationApproval).filter(
                EliminationApproval.entry_id == entry.id
            ).order_by(EliminationApproval.created_at).all()

            current_step = None
            approval_detail = []
            for a in approvals:
                approval_detail.append({
                    "role": a.approver_role,
                    "status": a.status.value,
                    "approver": a.approver_name or "",
                    "comment": a.comment or "",
                    "approved_at": a.approved_at.isoformat() if a.approved_at else "",
                })
                if a.status == ApprovalStatus.PENDING and current_step is None:
                    current_step = a.approver_role

            results.append({
                "entry_id": entry.id,
                "period": entry.period,
                "entry_type": entry.entry_type,
                "amount": float(entry.amount),
                "description": entry.description,
                "created_by": entry.created_by,
                "current_step": current_step or "已完成",
                "approval_detail": approval_detail,
                "debit_company": entry.debit_company,
                "credit_company": entry.credit_company,
            })
        return results

    def get_elimination_entries(self, period: str, is_manual: bool = None) -> List[EliminationEntry]:
        query = self.session.query(EliminationEntry).filter_by(period=period)
        if is_manual is not None:
            query = query.filter_by(is_manual=is_manual)
        return query.all()

    def _log_operation(self, op_type: str, target_id: str, detail: str,
                       operator: str = "system", is_anomaly: bool = False):
        log = OperationLog(
            id=str(uuid.uuid4()),
            operation_type=op_type,
            operator=operator,
            target_type="consolidation",
            target_id=target_id or "",
            detail=detail,
            is_anomaly=is_anomaly,
        )
        self.session.add(log)
        self.session.commit()
