import uuid
import json
import logging
import os
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from models import (
    SessionLocal, ConsolidationWorksheet, EliminationEntry,
    ConsolidatedReport, TrialBalance, Subsidiary, ApprovalStatus, OperationLog,
    DiscrepancyWorkOrder, WorkOrderStatus, EliminationApproval, ReportSubmission
)
from config import APPROVAL_FLOW

logger = logging.getLogger(__name__)

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


class ReportGenerator:
    """合并报表生成器 - 三大报表及附注, 支持PDF/Excel导出"""

    BALANCE_SHEET_ACCOUNTS = {
        "assets_current": ["1001", "1002", "1012", "1101", "1121", "1122", "1131", "1221", "1231", "1401", "1402", "1403", "1405", "1406", "1407", "1411", "1421", "1431", "1441", "1471"],
        "assets_noncurrent": ["1511", "1521", "1531", "1601", "1602", "1603", "1604", "1605", "1606", "1701", "1711", "1801"],
        "liabilities_current": ["2001", "2002", "2101", "2201", "2202", "2211", "2221", "2231", "2232", "2241", "2311", "2314", "2401"],
        "liabilities_noncurrent": ["2501", "2502", "2601", "2701"],
        "equity": ["4001", "4002", "4101", "4103", "4104"],
    }

    INCOME_STATEMENT_ACCOUNTS = {
        "revenue": ["6001", "6011", "6051"],
        "cost": ["6401", "6402", "6403"],
        "tax_surcharge": ["6405"],
        "selling_expense": ["6601"],
        "admin_expense": ["6602"],
        "finance_expense": ["6603"],
        "asset_impairment": ["6701"],
        "other_income": ["6301", "6711"],
        "other_expense": ["6711"],
        "income_tax": ["6801"],
    }

    CASH_FLOW_CATEGORIES = {
        "operating": "经营活动",
        "investing": "投资活动",
        "financing": "筹资活动",
    }

    def __init__(self, session: Session = None):
        self.session = session or SessionLocal()
        os.makedirs(OUTPUT_DIR, exist_ok=True)

    def generate_consolidated_balance_sheet(self, period: str) -> Dict:
        """生成合并资产负债表"""
        worksheets = self.session.query(ConsolidationWorksheet).filter_by(
            period=period
        ).all()
        eliminations = self.session.query(EliminationEntry).filter(
            EliminationEntry.period == period,
            EliminationEntry.approval_status == ApprovalStatus.APPROVED,
        ).all()

        ws_map: Dict[str, Decimal] = {}
        for ws in worksheets:
            key = f"{ws.company_code}|{ws.account_code}"
            ws_map[key] = ws.consolidated_amount

        elim_map: Dict[str, Decimal] = {}
        for e in eliminations:
            elim_map.setdefault(e.debit_account, Decimal("0"))
            elim_map[e.debit_account] -= e.amount
            elim_map.setdefault(e.credit_account, Decimal("0"))
            elim_map[e.credit_account] += e.amount

        report = {
            "period": period,
            "report_type": "consolidated_balance_sheet",
            "generated_at": datetime.utcnow().isoformat(),
            "assets": {},
            "liabilities": {},
            "equity": {},
            "minority_interest": Decimal("0"),
        }

        for category, codes in self.BALANCE_SHEET_ACCOUNTS.items():
            total = Decimal("0")
            for code_prefix in codes:
                for key, amount in ws_map.items():
                    parts = key.split("|")
                    acct_code = parts[1]
                    if acct_code.startswith(code_prefix[:3]):
                        total += amount
                if code_prefix in elim_map:
                    total += elim_map[code_prefix]

            if category.startswith("assets"):
                report["assets"][category] = float(total)
            elif category.startswith("liabilities"):
                report["liabilities"][category] = float(total)
            elif category == "equity":
                report["equity"][category] = float(total)

        minority_total = sum(
            ws.minority_interest_amount for ws in worksheets
            if ws.minority_interest_amount
        )
        report["minority_interest"] = float(minority_total)
        report["equity"]["minority_interest"] = float(minority_total)

        total_assets = sum(report["assets"].values())
        total_liabilities = sum(report["liabilities"].values())
        total_equity = sum(report["equity"].values())
        report["total_assets"] = total_assets
        report["total_liabilities_and_equity"] = total_liabilities + total_equity
        report["balance_check"] = abs(total_assets - total_liabilities - total_equity) < 0.01

        return report

    def generate_consolidated_income_statement(self, period: str) -> Dict:
        """生成合并利润表"""
        worksheets = self.session.query(ConsolidationWorksheet).filter_by(
            period=period
        ).all()
        eliminations = self.session.query(EliminationEntry).filter(
            EliminationEntry.period == period,
            EliminationEntry.approval_status == ApprovalStatus.APPROVED,
        ).all()

        elim_map: Dict[str, Decimal] = {}
        for e in eliminations:
            elim_map.setdefault(e.debit_account, Decimal("0"))
            elim_map[e.debit_account] -= e.amount
            elim_map.setdefault(e.credit_account, Decimal("0"))
            elim_map[e.credit_account] += e.amount

        ws_by_account: Dict[str, Decimal] = {}
        for ws in worksheets:
            ws_by_account.setdefault(ws.account_code, Decimal("0"))
            ws_by_account[ws.account_code] += ws.consolidated_amount

        report = {
            "period": period,
            "report_type": "consolidated_income_statement",
            "generated_at": datetime.utcnow().isoformat(),
            "items": {},
            "minority_interest_profit": Decimal("0"),
        }

        for category, codes in self.INCOME_STATEMENT_ACCOUNTS.items():
            total = Decimal("0")
            for code_prefix in codes:
                for acct_code, amount in ws_by_account.items():
                    if acct_code.startswith(code_prefix[:3]):
                        total += amount
                if code_prefix in elim_map:
                    total += elim_map[code_prefix]
            report["items"][category] = float(total)

        minority_profit = sum(
            ws.minority_interest_amount for ws in worksheets
            if ws.account_code and ws.account_code.startswith("5")
            and ws.minority_interest_amount
        )
        report["minority_interest_profit"] = float(minority_profit)

        revenue = report["items"].get("revenue", 0)
        cost = report["items"].get("cost", 0)
        report["gross_profit"] = revenue - cost
        report["net_profit"] = revenue - sum(v for k, v in report["items"].items() if k != "revenue")
        report["net_profit_attributable_parent"] = report["net_profit"] - float(minority_profit)

        return report

    def generate_consolidated_cash_flow(self, period: str) -> Dict:
        """生成合并现金流量表"""
        worksheets = self.session.query(ConsolidationWorksheet).filter_by(
            period=period
        ).all()

        report = {
            "period": period,
            "report_type": "consolidated_cash_flow_statement",
            "generated_at": datetime.utcnow().isoformat(),
            "operating_activities": {
                "net_cash_from_operations": 0,
                "adjustments": {},
                "net_cash_operating": 0,
            },
            "investing_activities": {
                "net_cash_from_investing": 0,
            },
            "financing_activities": {
                "net_cash_from_financing": 0,
            },
            "net_increase_cash": 0,
            "cash_beginning": 0,
            "cash_ending": 0,
        }

        operating = Decimal("0")
        investing = Decimal("0")
        financing = Decimal("0")
        cash_balance = Decimal("0")

        for ws in worksheets:
            code = ws.account_code
            amount = ws.consolidated_amount
            if code and code.startswith("100"):
                cash_balance += amount
            elif code and code.startswith(("112", "113", "122")):
                operating -= amount
            elif code and code.startswith(("151", "152", "153", "160")):
                investing -= amount
            elif code and code.startswith(("200", "210", "220", "250", "260")):
                financing += amount

        report["operating_activities"]["net_cash_operating"] = float(operating)
        report["investing_activities"]["net_cash_from_investing"] = float(investing)
        report["financing_activities"]["net_cash_from_financing"] = float(financing)
        report["net_increase_cash"] = float(operating + investing + financing)
        report["cash_ending"] = float(cash_balance)

        return report

    def generate_notes(self, period: str) -> Dict:
        """生成附注"""
        subsidiaries = self.session.query(Subsidiary).filter_by(is_active=True).all()
        eliminations = self.session.query(EliminationEntry).filter(
            EliminationEntry.period == period,
            EliminationEntry.approval_status == ApprovalStatus.APPROVED,
        ).all()
        worksheets = self.session.query(ConsolidationWorksheet).filter_by(period=period).all()

        notes = {
            "period": period,
            "report_type": "notes_to_financial_statements",
            "generated_at": datetime.utcnow().isoformat(),
            "company_listing": [
                {
                    "code": sub.code,
                    "name": sub.name,
                    "ownership_ratio": float(sub.ownership_ratio),
                }
                for sub in subsidiaries
            ],
            "elimination_summary": [
                {
                    "type": e.entry_type,
                    "debit_company": e.debit_company,
                    "credit_company": e.credit_company,
                    "amount": float(e.amount),
                    "description": e.description,
                    "is_manual": e.is_manual,
                }
                for e in eliminations
            ],
            "minority_interest_detail": [],
            "accounting_policies": "本合并报表按照中国企业会计准则编制",
            "significant_events": [],
        }

        minority_data = {}
        for ws in worksheets:
            if ws.minority_interest_amount and ws.minority_interest_amount != Decimal("0"):
                minority_data.setdefault(ws.company_code, Decimal("0"))
                minority_data[ws.company_code] += ws.minority_interest_amount

        for company_code, amount in minority_data.items():
            sub = self.session.query(Subsidiary).filter_by(code=company_code).first()
            notes["minority_interest_detail"].append({
                "company_code": company_code,
                "company_name": sub.name if sub else company_code,
                "amount": float(amount),
            })

        return notes

    def check_report_risks(self, period: str) -> List[Dict]:
        risks = []

        pending_entries = self.session.query(EliminationEntry).filter(
            EliminationEntry.period == period,
            EliminationEntry.requires_approval == True,
            EliminationEntry.approval_status == ApprovalStatus.PENDING,
        ).all()
        for entry in pending_entries:
            risks.append({
                "level": "high",
                "type": "pending_approval",
                "description": f"手工抵消分录(ID={entry.id[:8]})金额{float(entry.amount):,.2f}元待审批, 未审批不计入报表",
                "amount": float(entry.amount),
                "id": entry.id,
            })

        major_open_orders = self.session.query(DiscrepancyWorkOrder).filter(
            DiscrepancyWorkOrder.status.in_([
                WorkOrderStatus.OPEN, WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.ESCALATED
            ]),
            DiscrepancyWorkOrder.discrepancy_amount >= 100000,
        ).all()
        for wo in major_open_orders:
            status_label = "升级中" if wo.status == WorkOrderStatus.ESCALATED else "未解决"
            risks.append({
                "level": "high",
                "type": "unresolved_discrepancy",
                "description": f"{status_label}差异工单(ID={wo.id[:8]})差异金额{float(wo.discrepancy_amount or 0):,.2f}元, 类型={wo.discrepancy_type}",
                "amount": float(wo.discrepancy_amount or 0),
                "id": wo.id,
            })

        subsidiaries = self.session.query(Subsidiary).filter_by(is_active=True).all()
        for sub in subsidiaries:
            tb_count = self.session.query(TrialBalance).filter(
                TrialBalance.company_code == sub.code,
                TrialBalance.period == period,
            ).count()
            if tb_count == 0:
                risks.append({
                    "level": "high",
                    "type": "missing_trial_balance",
                    "description": f"子公司{sub.name}({sub.code})未提交{period}期间试算平衡表",
                    "amount": 0,
                    "id": sub.code,
                })

        return risks

    def export_to_excel(self, period: str, report_type: str = "all",
                        is_draft: bool = False) -> str:
        """导出合并报表为Excel"""
        try:
            import openpyxl
            from openpyxl.styles import Font, Alignment, Border, Side, PatternFill
        except ImportError:
            logger.error("openpyxl未安装, 请执行 pip install openpyxl")
            return ""

        wb = openpyxl.Workbook()

        header_font = Font(name="微软雅黑", size=14, bold=True)
        sub_header_font = Font(name="微软雅黑", size=11, bold=True)
        data_font = Font(name="微软雅黑", size=10)
        header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
        header_font_white = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
        border = Border(
            left=Side(style="thin"),
            right=Side(style="thin"),
            top=Side(style="thin"),
            bottom=Side(style="thin"),
        )
        num_format = '#,##0.00'

        if report_type in ("all", "balance_sheet"):
            bs = self.generate_consolidated_balance_sheet(period)
            ws = wb.active
            ws.title = "合并资产负债表"
            self._write_balance_sheet_to_excel(ws, bs, header_font, sub_header_font,
                                                data_font, header_fill, header_font_white,
                                                border, num_format)

        if report_type in ("all", "income_statement"):
            pl = self.generate_consolidated_income_statement(period)
            ws2 = wb.create_sheet("合并利润表")
            self._write_income_statement_to_excel(ws2, pl, header_font, sub_header_font,
                                                   data_font, header_fill, header_font_white,
                                                   border, num_format)

        if report_type in ("all", "cash_flow"):
            cf = self.generate_consolidated_cash_flow(period)
            ws3 = wb.create_sheet("合并现金流量表")
            self._write_cash_flow_to_excel(ws3, cf, header_font, sub_header_font,
                                            data_font, header_fill, header_font_white,
                                            border, num_format)

        if report_type == "all":
            notes = self.generate_notes(period)
            ws4 = wb.create_sheet("附注")
            self._write_notes_to_excel(ws4, notes, header_font, sub_header_font,
                                        data_font, header_fill, header_font_white,
                                        border, num_format)

        filename = f"合并报表_{period}_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
        if is_draft:
            filename = f"合并报表_{period}_草稿_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
        filepath = os.path.join(OUTPUT_DIR, filename)
        wb.save(filepath)
        logger.info(f"Excel导出: {filepath}")

        risks = self.check_report_risks(period)
        self._save_report_record(period, report_type, excel_path=filepath,
                                  is_draft=is_draft, risk_items=risks)
        return filepath

    def export_to_pdf(self, period: str, report_type: str = "all",
                      is_draft: bool = False) -> str:
        """导出合并报表为PDF (带图表)"""
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.units import mm
            from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib import colors
            from reportlab.lib.enums import TA_CENTER
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
        except ImportError:
            logger.error("reportlab未安装, 请执行 pip install reportlab")
            return ""

        filename = f"合并报表_{period}_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"
        if is_draft:
            filename = f"合并报表_{period}_草稿_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"
        filepath = os.path.join(OUTPUT_DIR, filename)

        doc = SimpleDocTemplate(filepath, pagesize=A4)
        elements = []

        styles = getSampleStyleSheet()
        try:
            styles.add(ParagraphStyle(
                name="ChineseTitle",
                parent=styles["Title"],
                fontSize=16,
                alignment=TA_CENTER,
                spaceAfter=20,
            ))
        except Exception:
            pass

        if report_type in ("all", "balance_sheet"):
            bs = self.generate_consolidated_balance_sheet(period)
            elements.append(Paragraph("合并资产负债表", styles["Title"]))
            elements.append(Spacer(1, 10))
            table_data = [["项目", "金额(元)"]]
            for key, val in bs.get("assets", {}).items():
                table_data.append([f"资产-{key}", f"{val:,.2f}"])
            for key, val in bs.get("liabilities", {}).items():
                table_data.append([f"负债-{key}", f"{val:,.2f}"])
            for key, val in bs.get("equity", {}).items():
                table_data.append([f"权益-{key}", f"{val:,.2f}"])
            table_data.append(["资产总计", f"{bs.get('total_assets', 0):,.2f}"])
            table_data.append(["负债和权益总计", f"{bs.get('total_liabilities_and_equity', 0):,.2f}"])

            t = Table(table_data, colWidths=[300, 200])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4472C4")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ]))
            elements.append(t)
            elements.append(Spacer(1, 20))

        if report_type in ("all", "income_statement"):
            pl = self.generate_consolidated_income_statement(period)
            elements.append(Paragraph("合并利润表", styles["Title"]))
            elements.append(Spacer(1, 10))
            table_data = [["项目", "金额(元)"]]
            for key, val in pl.get("items", {}).items():
                table_data.append([key, f"{val:,.2f}"])
            table_data.append(["毛利润", f"{pl.get('gross_profit', 0):,.2f}"])
            table_data.append(["净利润", f"{pl.get('net_profit', 0):,.2f}"])
            table_data.append(["归属于母公司净利润", f"{pl.get('net_profit_attributable_parent', 0):,.2f}"])
            table_data.append(["少数股东损益", f"{pl.get('minority_interest_profit', 0):,.2f}"])

            t = Table(table_data, colWidths=[300, 200])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4472C4")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ]))
            elements.append(t)
            elements.append(Spacer(1, 20))

        if report_type in ("all", "cash_flow"):
            cf = self.generate_consolidated_cash_flow(period)
            elements.append(Paragraph("合并现金流量表", styles["Title"]))
            elements.append(Spacer(1, 10))
            table_data = [["项目", "金额(元)"]]
            table_data.append(["一、经营活动现金流量净额",
                                f"{cf['operating_activities']['net_cash_operating']:,.2f}"])
            table_data.append(["二、投资活动现金流量净额",
                                f"{cf['investing_activities']['net_cash_from_investing']:,.2f}"])
            table_data.append(["三、筹资活动现金流量净额",
                                f"{cf['financing_activities']['net_cash_from_financing']:,.2f}"])
            table_data.append(["现金净增加额", f"{cf['net_increase_cash']:,.2f}"])
            table_data.append(["期末现金余额", f"{cf['cash_ending']:,.2f}"])

            t = Table(table_data, colWidths=[300, 200])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4472C4")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ]))
            elements.append(t)
            elements.append(Spacer(1, 20))

        if report_type == "all":
            notes = self.generate_notes(period)
            elements.append(Paragraph("附注", styles["Title"]))
            elements.append(Spacer(1, 10))

            elements.append(Paragraph("一、合并范围", styles["Heading2"]))
            elements.append(Spacer(1, 5))
            comp_data = [["编码", "名称", "持股比例"]]
            for comp in notes.get("company_listing", []):
                comp_data.append([comp["code"], comp["name"], f"{comp['ownership_ratio']:.1%}"])
            t = Table(comp_data, colWidths=[100, 200, 100])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4472C4")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
            ]))
            elements.append(t)
            elements.append(Spacer(1, 15))

            elements.append(Paragraph("二、抵消分录汇总", styles["Heading2"]))
            elements.append(Spacer(1, 5))
            elim_data = [["类型", "借方公司", "贷方公司", "金额(元)", "说明"]]
            for elim in notes.get("elimination_summary", []):
                elim_data.append([
                    elim["type"],
                    elim["debit_company"],
                    elim["credit_company"],
                    f"{elim['amount']:,.2f}",
                    (elim.get("description") or "")[:30],
                ])
            if len(elim_data) > 1:
                t = Table(elim_data, colWidths=[80, 70, 70, 80, 150])
                t.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4472C4")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("FONTSIZE", (0, 0), (-1, -1), 7),
                    ("ALIGN", (3, 0), (3, -1), "RIGHT"),
                ]))
                elements.append(t)
            else:
                elements.append(Paragraph("无抵消分录", styles["Normal"]))
            elements.append(Spacer(1, 15))

            elements.append(Paragraph("三、少数股东权益明细", styles["Heading2"]))
            elements.append(Spacer(1, 5))
            mi_data = [["子公司", "金额(元)"]]
            for mi in notes.get("minority_interest_detail", []):
                mi_data.append([mi["company_name"], f"{mi['amount']:,.2f}"])
            if len(mi_data) > 1:
                t = Table(mi_data, colWidths=[200, 200])
                t.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4472C4")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ]))
                elements.append(t)
            else:
                elements.append(Paragraph("无少数股东权益", styles["Normal"]))
            elements.append(Spacer(1, 15))

            elements.append(Paragraph("四、会计政策", styles["Heading2"]))
            elements.append(Spacer(1, 5))
            elements.append(Paragraph(notes.get("accounting_policies", ""), styles["Normal"]))

        chart_path = self._generate_chart(period)
        if chart_path and os.path.exists(chart_path):
            try:
                elements.append(Paragraph("合并报表趋势图", styles["Title"]))
                elements.append(Spacer(1, 10))
                elements.append(Image(chart_path, width=450, height=300))
            except Exception as e:
                logger.warning(f"PDF嵌入图表失败: {e}")

        doc.build(elements)
        logger.info(f"PDF导出: {filepath}")

        risks = self.check_report_risks(period)
        self._save_report_record(period, report_type, pdf_path=filepath,
                                  is_draft=is_draft, risk_items=risks)
        return filepath

    def _generate_chart(self, period: str) -> str:
        """生成图表"""
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "SimHei", "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
        except ImportError:
            logger.warning("matplotlib未安装, 跳过图表生成")
            return ""

        bs = self.generate_consolidated_balance_sheet(period)

        fig, axes = plt.subplots(1, 2, figsize=(12, 5))

        asset_labels = list(bs.get("assets", {}).keys())
        asset_values = list(bs.get("assets", {}).values())
        axes[0].bar(asset_labels, asset_values, color=["#4472C4", "#ED7D31"])
        axes[0].set_title("资产构成")
        axes[0].set_ylabel("金额(元)")

        le_data = {
            **{f"负债-{k}": v for k, v in bs.get("liabilities", {}).items()},
            **{f"权益-{k}": v for k, v in bs.get("equity", {}).items()},
        }
        axes[1].pie(le_data.values(), labels=le_data.keys(), autopct="%1.1f%%")
        axes[1].set_title("负债与权益结构")

        plt.tight_layout()
        chart_path = os.path.join(OUTPUT_DIR, f"chart_{period}.png")
        plt.savefig(chart_path, dpi=150, bbox_inches="tight")
        plt.close()
        return chart_path

    def _write_balance_sheet_to_excel(self, ws, data, *styles):
        header_font, sub_header_font, data_font, header_fill, header_font_white, border, num_format = styles
        ws.merge_cells("A1:B1")
        ws["A1"] = f"合并资产负债表 - {data['period']}"
        ws["A1"].font = header_font
        ws.append(["项目", "金额(元)"])
        for cell in ws[2]:
            cell.font = header_font_white
            cell.fill = header_fill
            cell.border = border
        row = 3
        ws.cell(row=row, column=1, value="一、资产").font = sub_header_font
        row += 1
        for key, val in data.get("assets", {}).items():
            ws.cell(row=row, column=1, value=f"  {key}").font = data_font
            c = ws.cell(row=row, column=2, value=val)
            c.font = data_font
            c.number_format = num_format
            row += 1
        ws.cell(row=row, column=1, value="资产合计").font = sub_header_font
        c = ws.cell(row=row, column=2, value=data.get("total_assets", 0))
        c.font = sub_header_font
        c.number_format = num_format
        row += 2
        ws.cell(row=row, column=1, value="二、负债").font = sub_header_font
        row += 1
        for key, val in data.get("liabilities", {}).items():
            ws.cell(row=row, column=1, value=f"  {key}").font = data_font
            c = ws.cell(row=row, column=2, value=val)
            c.font = data_font
            c.number_format = num_format
            row += 1
        row += 1
        ws.cell(row=row, column=1, value="三、所有者权益").font = sub_header_font
        row += 1
        for key, val in data.get("equity", {}).items():
            ws.cell(row=row, column=1, value=f"  {key}").font = data_font
            c = ws.cell(row=row, column=2, value=val)
            c.font = data_font
            c.number_format = num_format
            row += 1
        ws.column_dimensions["A"].width = 35
        ws.column_dimensions["B"].width = 20

    def _write_income_statement_to_excel(self, ws, data, *styles):
        header_font, sub_header_font, data_font, header_fill, header_font_white, border, num_format = styles
        ws.merge_cells("A1:B1")
        ws["A1"] = f"合并利润表 - {data['period']}"
        ws["A1"].font = header_font
        ws.append(["项目", "金额(元)"])
        for cell in ws[2]:
            cell.font = header_font_white
            cell.fill = header_fill
            cell.border = border
        row = 3
        for key, val in data.get("items", {}).items():
            ws.cell(row=row, column=1, value=key).font = data_font
            c = ws.cell(row=row, column=2, value=val)
            c.font = data_font
            c.number_format = num_format
            row += 1
        ws.cell(row=row, column=1, value="毛利润").font = sub_header_font
        ws.cell(row=row, column=2, value=data.get("gross_profit", 0)).font = sub_header_font
        row += 1
        ws.cell(row=row, column=1, value="净利润").font = sub_header_font
        ws.cell(row=row, column=2, value=data.get("net_profit", 0)).font = sub_header_font
        row += 1
        ws.cell(row=row, column=1, value="归属于母公司净利润").font = sub_header_font
        ws.cell(row=row, column=2, value=data.get("net_profit_attributable_parent", 0)).font = sub_header_font
        row += 1
        ws.cell(row=row, column=1, value="少数股东损益").font = sub_header_font
        ws.cell(row=row, column=2, value=data.get("minority_interest_profit", 0)).font = sub_header_font
        ws.column_dimensions["A"].width = 30
        ws.column_dimensions["B"].width = 20

    def _write_cash_flow_to_excel(self, ws, data, *styles):
        header_font, sub_header_font, data_font, header_fill, header_font_white, border, num_format = styles
        ws.merge_cells("A1:B1")
        ws["A1"] = f"合并现金流量表 - {data['period']}"
        ws["A1"].font = header_font
        ws.append(["项目", "金额(元)"])
        for cell in ws[2]:
            cell.font = header_font_white
            cell.fill = header_fill
            cell.border = border
        row = 3
        ws.cell(row=row, column=1, value="一、经营活动").font = sub_header_font
        ws.cell(row=row, column=2, value=data["operating_activities"]["net_cash_operating"]).font = data_font
        row += 1
        ws.cell(row=row, column=1, value="二、投资活动").font = sub_header_font
        ws.cell(row=row, column=2, value=data["investing_activities"]["net_cash_from_investing"]).font = data_font
        row += 1
        ws.cell(row=row, column=1, value="三、筹资活动").font = sub_header_font
        ws.cell(row=row, column=2, value=data["financing_activities"]["net_cash_from_financing"]).font = data_font
        row += 1
        ws.cell(row=row, column=1, value="现金净增加额").font = sub_header_font
        ws.cell(row=row, column=2, value=data["net_increase_cash"]).font = sub_header_font
        row += 1
        ws.cell(row=row, column=1, value="期末现金余额").font = sub_header_font
        ws.cell(row=row, column=2, value=data["cash_ending"]).font = sub_header_font
        ws.column_dimensions["A"].width = 30
        ws.column_dimensions["B"].width = 20

    def _write_notes_to_excel(self, ws, data, *styles):
        header_font, sub_header_font, data_font, header_fill, header_font_white, border, num_format = styles
        ws.merge_cells("A1:C1")
        ws["A1"] = f"附注 - {data['period']}"
        ws["A1"].font = header_font
        row = 3
        ws.cell(row=row, column=1, value="一、合并范围").font = sub_header_font
        row += 1
        for comp in data.get("company_listing", []):
            ws.cell(row=row, column=1, value=comp["code"]).font = data_font
            ws.cell(row=row, column=2, value=comp["name"]).font = data_font
            ws.cell(row=row, column=3, value=f"{comp['ownership_ratio']:.1%}").font = data_font
            row += 1
        row += 1
        ws.cell(row=row, column=1, value="二、抵消分录汇总").font = sub_header_font
        row += 1
        for elim in data.get("elimination_summary", []):
            ws.cell(row=row, column=1, value=elim["type"]).font = data_font
            ws.cell(row=row, column=2, value=elim["description"]).font = data_font
            c = ws.cell(row=row, column=3, value=elim["amount"])
            c.font = data_font
            c.number_format = num_format
            row += 1
        row += 1
        ws.cell(row=row, column=1, value="三、少数股东权益").font = sub_header_font
        row += 1
        for mi in data.get("minority_interest_detail", []):
            ws.cell(row=row, column=1, value=mi["company_name"]).font = data_font
            c = ws.cell(row=row, column=2, value=mi["amount"])
            c.font = data_font
            c.number_format = num_format
            row += 1
        row += 1
        ws.cell(row=row, column=1, value="四、会计政策").font = sub_header_font
        row += 1
        policy = data.get("accounting_policies", "")
        if policy:
            ws.cell(row=row, column=1, value=policy).font = data_font
            row += 1
        ws.column_dimensions["A"].width = 25
        ws.column_dimensions["B"].width = 40
        ws.column_dimensions["C"].width = 20

    def _save_report_record(self, period: str, report_type: str,
                            pdf_path: str = None, excel_path: str = None,
                            is_draft: bool = False, risk_items: List = None):
        report_status = "draft" if is_draft else "published"
        record = ConsolidatedReport(
            id=str(uuid.uuid4()),
            period=period,
            report_type=report_type,
            file_path_pdf=pdf_path,
            file_path_excel=excel_path,
            is_draft=is_draft,
            risk_items=json.dumps(risk_items or [], ensure_ascii=False),
            generated_by="system",
            report_status=report_status,
        )
        self.session.add(record)
        self.session.commit()
        return record

    def publish_report(self, report_id: str, published_by: str) -> Optional[ConsolidatedReport]:
        draft = self.session.query(ConsolidatedReport).filter_by(id=report_id).first()
        if not draft:
            return None
        if draft.report_status != "draft":
            return None

        risks = self.check_report_risks(draft.period)
        if risks:
            return None

        published = ConsolidatedReport(
            id=str(uuid.uuid4()),
            period=draft.period,
            report_type=draft.report_type,
            file_path_pdf=draft.file_path_pdf,
            file_path_excel=draft.file_path_excel,
            is_draft=False,
            risk_items=draft.risk_items,
            generated_at=draft.generated_at,
            generated_by=draft.generated_by,
            report_status="published",
            published_by=published_by,
            published_at=datetime.utcnow(),
            draft_source_id=draft.id,
        )
        self.session.add(published)
        self.session.commit()

        log = OperationLog(
            id=str(uuid.uuid4()),
            operation_type="publish_report",
            operator=published_by,
            target_type="consolidated_report",
            target_id=published.id,
            detail=f"发布正式版报表, 期间={draft.period}, 草稿ID={draft.id[:8]}",
        )
        self.session.add(log)
        self.session.commit()
        return published

    def revoke_report(self, report_id: str, revoked_by: str, reason: str) -> Optional[ConsolidatedReport]:
        report = self.session.query(ConsolidatedReport).filter_by(id=report_id).first()
        if not report:
            return None
        if report.report_status != "published":
            return None

        report.report_status = "revoked"
        report.revoked_by = revoked_by
        report.revoked_at = datetime.utcnow()
        report.revoke_reason = reason
        self.session.commit()

        log = OperationLog(
            id=str(uuid.uuid4()),
            operation_type="revoke_report",
            operator=revoked_by,
            target_type="consolidated_report",
            target_id=report.id,
            detail=f"撤回正式版报表, 期间={report.period}, 原因={reason}",
        )
        self.session.add(log)
        self.session.commit()
        return report
