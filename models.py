import enum
from datetime import datetime
from sqlalchemy import (
    Column, String, Float, Integer, DateTime, Enum, Text,
    ForeignKey, Boolean, Date, Numeric, create_engine, Index
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker
from config import DB_URL

Base = declarative_base()
engine = create_engine(DB_URL, pool_pre_ping=True, pool_size=20, max_overflow=10)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class MatchStatus(enum.Enum):
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    PARTIAL = "partial"
    EXEMPTED = "exempted"


class WorkOrderStatus(enum.Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    CLOSED = "closed"


class EscalationLevel(enum.Enum):
    LEVEL_1 = "子公司CFO"
    LEVEL_2 = "集团财务总监"
    LEVEL_3 = "CEO"


class ApprovalStatus(enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ReportStatus(enum.Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    VERIFIED = "verified"
    REJECTED = "rejected"


class BalanceCheckStatus(enum.Enum):
    BALANCED = "balanced"
    UNBALANCED = "unbalanced"


class Subsidiary(Base):
    __tablename__ = "subsidiaries"
    id = Column(String(36), primary_key=True)
    name = Column(String(200), nullable=False)
    code = Column(String(50), unique=True, nullable=False)
    ownership_ratio = Column(Numeric(5, 4), nullable=False, default=1.0)
    cfo_name = Column(String(100))
    cfo_contact = Column(String(200))
    finance_staff = Column(Text)
    data_source_type = Column(String(20), default="csv")
    data_source_url = Column(String(500), default="")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    transactions = relationship("InternalTransaction", back_populates="subsidiary")
    trial_balances = relationship("TrialBalance", back_populates="subsidiary")
    report_submissions = relationship("ReportSubmission", back_populates="subsidiary")


class InternalTransaction(Base):
    __tablename__ = "internal_transactions"
    __table_args__ = (
        Index("ix_trans_comp_date", "company_code", "transaction_date"),
        Index("ix_trans_counter_date", "counterparty_code", "transaction_date"),
        Index("ix_trans_match", "match_status", "transaction_date"),
    )
    id = Column(String(36), primary_key=True)
    company_code = Column(String(50), ForeignKey("subsidiaries.code"), nullable=False)
    company_name = Column(String(200), nullable=False)
    counterparty_code = Column(String(50), nullable=False)
    counterparty_name = Column(String(200), nullable=False)
    transaction_date = Column(Date, nullable=False)
    product_code = Column(String(100), nullable=False)
    product_name = Column(String(200))
    amount = Column(Numeric(18, 2), nullable=False)
    quantity = Column(Numeric(18, 2))
    direction = Column(String(10), nullable=False)
    match_status = Column(Enum(MatchStatus), default=MatchStatus.UNMATCHED)
    matched_transaction_id = Column(String(36))
    match_score = Column(Float)
    batch_id = Column(String(36))
    source_system = Column(String(100))
    raw_data = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    subsidiary = relationship("Subsidiary", back_populates="transactions")


class DiscrepancyWorkOrder(Base):
    __tablename__ = "discrepancy_work_orders"
    __table_args__ = (
        Index("ix_wo_status", "status", "created_at"),
    )
    id = Column(String(36), primary_key=True)
    transaction_id_buy = Column(String(36), nullable=False)
    transaction_id_sell = Column(String(36))
    discrepancy_type = Column(String(50), nullable=False)
    discrepancy_amount = Column(Numeric(18, 2))
    discrepancy_description = Column(Text)
    responsible_company_code = Column(String(50), nullable=False)
    assigned_to = Column(String(100))
    status = Column(Enum(WorkOrderStatus), default=WorkOrderStatus.OPEN)
    escalation_level = Column(Enum(EscalationLevel))
    escalated_at = Column(DateTime)
    resolved_at = Column(DateTime)
    resolution_note = Column(Text)
    category = Column(String(50))
    processing_notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    approval_records = relationship("ApprovalRecord", back_populates="work_order")


class ApprovalRecord(Base):
    __tablename__ = "approval_records"
    id = Column(String(36), primary_key=True)
    work_order_id = Column(String(36), ForeignKey("discrepancy_work_orders.id"))
    approval_type = Column(String(50), nullable=False)
    approver_role = Column(String(50), nullable=False)
    approver_name = Column(String(100))
    status = Column(Enum(ApprovalStatus), default=ApprovalStatus.PENDING)
    comment = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    approved_at = Column(DateTime)
    work_order = relationship("DiscrepancyWorkOrder", back_populates="approval_records")


class TrialBalance(Base):
    __tablename__ = "trial_balances"
    __table_args__ = (
        Index("ix_tb_company_period", "company_code", "period"),
    )
    id = Column(String(36), primary_key=True)
    company_code = Column(String(50), ForeignKey("subsidiaries.code"), nullable=False)
    period = Column(String(7), nullable=False)
    account_code = Column(String(50), nullable=False)
    account_name = Column(String(200), nullable=False)
    account_type = Column(String(20), nullable=False)
    opening_balance = Column(Numeric(18, 2), default=0)
    debit_amount = Column(Numeric(18, 2), default=0)
    credit_amount = Column(Numeric(18, 2), default=0)
    closing_balance = Column(Numeric(18, 2), default=0)
    is_balanced = Column(Enum(BalanceCheckStatus), default=BalanceCheckStatus.BALANCED)
    imbalance_amount = Column(Numeric(18, 2), default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    subsidiary = relationship("Subsidiary", back_populates="trial_balances")


class ConsolidationWorksheet(Base):
    __tablename__ = "consolidation_worksheets"
    id = Column(String(36), primary_key=True)
    period = Column(String(7), nullable=False)
    company_code = Column(String(50), nullable=False)
    account_code = Column(String(50), nullable=False)
    account_name = Column(String(200), nullable=False)
    original_amount = Column(Numeric(18, 2), default=0)
    adjustment_amount = Column(Numeric(18, 2), default=0)
    consolidated_amount = Column(Numeric(18, 2), default=0)
    minority_interest_amount = Column(Numeric(18, 2), default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class EliminationEntry(Base):
    __tablename__ = "elimination_entries"
    __table_args__ = (
        Index("ix_elim_period", "period", "is_manual"),
    )
    id = Column(String(36), primary_key=True)
    period = Column(String(7), nullable=False)
    entry_type = Column(String(50), nullable=False)
    debit_company = Column(String(50), nullable=False)
    debit_account = Column(String(50), nullable=False)
    credit_company = Column(String(50), nullable=False)
    credit_account = Column(String(50), nullable=False)
    amount = Column(Numeric(18, 2), nullable=False)
    description = Column(Text)
    is_manual = Column(Boolean, default=False)
    requires_approval = Column(Boolean, default=False)
    approval_status = Column(Enum(ApprovalStatus), default=ApprovalStatus.APPROVED)
    created_by = Column(String(100))
    created_at = Column(DateTime, default=datetime.utcnow)
    approval_records = relationship("EliminationApproval", back_populates="entry")


class EliminationApproval(Base):
    __tablename__ = "elimination_approvals"
    id = Column(String(36), primary_key=True)
    entry_id = Column(String(36), ForeignKey("elimination_entries.id"))
    approver_role = Column(String(50), nullable=False)
    approver_name = Column(String(100))
    status = Column(Enum(ApprovalStatus), default=ApprovalStatus.PENDING)
    comment = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    approved_at = Column(DateTime)
    entry = relationship("EliminationEntry", back_populates="approval_records")


class ReportSubmission(Base):
    __tablename__ = "report_submissions"
    __table_args__ = (
        Index("ix_rs_company_period", "company_code", "period"),
    )
    id = Column(String(36), primary_key=True)
    company_code = Column(String(50), ForeignKey("subsidiaries.code"), nullable=False)
    period = Column(String(7), nullable=False)
    report_type = Column(String(50), nullable=False)
    status = Column(Enum(ReportStatus), default=ReportStatus.PENDING)
    submitted_at = Column(DateTime)
    submitted_by = Column(String(100))
    is_late = Column(Boolean, default=False)
    reminder_count = Column(Integer, default=0)
    last_reminder_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    subsidiary = relationship("Subsidiary", back_populates="report_submissions")


class ConsolidatedReport(Base):
    __tablename__ = "consolidated_reports"
    id = Column(String(36), primary_key=True)
    period = Column(String(7), nullable=False)
    report_type = Column(String(50), nullable=False)
    file_path_pdf = Column(String(500))
    file_path_excel = Column(String(500))
    is_draft = Column(Boolean, default=False)
    risk_items = Column(Text)
    generated_at = Column(DateTime, default=datetime.utcnow)
    generated_by = Column(String(100))


class OperationLog(Base):
    __tablename__ = "operation_logs"
    __table_args__ = (
        Index("ix_oplog_time", "created_at"),
        Index("ix_oplog_type", "operation_type"),
    )
    id = Column(String(36), primary_key=True)
    operation_type = Column(String(50), nullable=False)
    operator = Column(String(100))
    target_type = Column(String(50))
    target_id = Column(String(36))
    detail = Column(Text)
    is_anomaly = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class QuarterlyReport(Base):
    __tablename__ = "quarterly_reports"
    id = Column(String(36), primary_key=True)
    year = Column(Integer, nullable=False)
    quarter = Column(Integer, nullable=False)
    reconciliation_completion_rate = Column(Numeric(5, 2))
    discrepancy_rate = Column(Numeric(5, 2))
    avg_processing_hours = Column(Numeric(8, 2))
    total_transactions = Column(Integer)
    matched_transactions = Column(Integer)
    discrepancy_count = Column(Integer)
    file_path_pdf = Column(String(500))
    file_path_excel = Column(String(500))
    generated_at = Column(DateTime, default=datetime.utcnow)


class FetchBatch(Base):
    __tablename__ = "fetch_batches"
    __table_args__ = (
        Index("ix_fb_company_date", "company_code", "fetch_date"),
    )
    id = Column(String(36), primary_key=True)
    company_code = Column(String(50), nullable=False)
    company_name = Column(String(200), nullable=False)
    fetch_date = Column(Date, nullable=False)
    status = Column(String(20), nullable=False)
    record_count = Column(Integer, default=0)
    error_message = Column(Text)
    duration_seconds = Column(Float, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


def init_db():
    Base.metadata.create_all(engine)


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
