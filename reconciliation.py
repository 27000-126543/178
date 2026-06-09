import uuid
import json
import csv
import logging
import os
import urllib.request
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import and_, or_

from models import (
    SessionLocal, InternalTransaction, MatchStatus,
    DiscrepancyWorkOrder, WorkOrderStatus, EscalationLevel, Subsidiary,
    FetchBatch
)
from config import CONCURRENT_WORKERS, BATCH_SIZE, DIFFERENCE_TOLERANCE

logger = logging.getLogger(__name__)


class TransactionFetcher:
    """从各子公司业务系统抓取内部交易流水 - 支持每家公司配置CSV或HTTP数据源"""

    def __init__(self, session: Session = None):
        self.session = session or SessionLocal()

    def fetch_from_csv(self, file_path: str, company_code: str) -> List[Dict]:
        transactions = []
        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                row["company_code"] = company_code
                transactions.append(row)
        return transactions

    def fetch_from_http(self, url: str, company_code: str) -> List[Dict]:
        transactions = []
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, list):
                    for row in data:
                        row["company_code"] = company_code
                        transactions.append(row)
                elif isinstance(data, dict):
                    for row in data.get("data", data.get("transactions", [])):
                        row["company_code"] = company_code
                        transactions.append(row)
        except Exception as e:
            logger.error(f"HTTP抓取失败 {url}: {e}")
            raise
        return transactions

    def _fetch_for_subsidiary(self, sub: Subsidiary, target_date: date) -> Tuple[str, int, str, float]:
        import time
        source_type = sub.data_source_type or "csv"
        source_url = sub.data_source_url or ""

        if not source_url:
            return sub.code, 0, "未配置数据源(data_source_url为空)", 0.0

        existing_batch = self.session.query(FetchBatch).filter(
            FetchBatch.company_code == sub.code,
            FetchBatch.fetch_date == target_date,
            FetchBatch.status == "success",
        ).first()
        if existing_batch:
            return sub.code, 0, f"该日期已成功导入{existing_batch.record_count}条, 跳过重复导入", 0.0

        start_time = time.time()
        try:
            if source_type == "http":
                url = source_url.replace("{date}", target_date.isoformat())
                transactions = self.fetch_from_http(url, sub.code)
            elif source_type == "csv":
                path = source_url.replace("{date}", target_date.isoformat())
                if not os.path.exists(path):
                    return sub.code, 0, f"文件不存在: {path}", 0.0
                transactions = self.fetch_from_csv(path, sub.code)
            else:
                return sub.code, 0, f"不支持的数据源类型: {source_type}", 0.0

            if not transactions:
                return sub.code, 0, "数据源返回0条记录(空数据)", time.time() - start_time

            before_count = transactions
            transactions = [t for t in transactions
                            if self._parse_tx_date(t.get("transaction_date")) == target_date]
            filtered_out = len(before_count) - len(transactions)
            filter_msg = f", 过滤非当日{filtered_out}条" if filtered_out > 0 else ""

            deduped = self._deduplicate_transactions(transactions, sub.code, target_date)
            dup_msg = f", 去重{len(transactions) - len(deduped)}条" if len(deduped) < len(transactions) else ""

            if not deduped:
                return sub.code, 0, f"当日数据0条{filter_msg}{dup_msg}", time.time() - start_time

            count = self.save_transactions(deduped)
            elapsed = time.time() - start_time
            return sub.code, count, f"成功{filter_msg}{dup_msg}", elapsed
        except Exception as e:
            elapsed = time.time() - start_time
            return sub.code, 0, f"失败: {e}", elapsed

    def _parse_tx_date(self, val) -> date:
        if isinstance(val, date):
            return val
        if isinstance(val, str):
            try:
                return date.fromisoformat(val)
            except ValueError:
                pass
        return date.today()

    def _deduplicate_transactions(self, transactions: List[Dict], company_code: str,
                                   target_date: date) -> List[Dict]:
        existing = self.session.query(InternalTransaction).filter(
            InternalTransaction.company_code == company_code,
            InternalTransaction.transaction_date == target_date,
        ).all()
        existing_keys = set()
        for tx in existing:
            key = f"{tx.counterparty_code}|{tx.product_code}|{tx.amount}|{tx.direction}"
            existing_keys.add(key)
        deduped = []
        seen_keys = set()
        for t in transactions:
            key = f"{t.get('counterparty_code', '')}|{t.get('product_code', '')}|{t.get('amount', 0)}|{t.get('direction', 'buy')}"
            if key in existing_keys or key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(t)
        return deduped

    def save_transactions(self, transactions: List[Dict], batch_id: str = None) -> int:
        if not batch_id:
            batch_id = str(uuid.uuid4())
        saved = 0
        for i in range(0, len(transactions), BATCH_SIZE):
            batch = transactions[i:i + BATCH_SIZE]
            objs = []
            for t in batch:
                tx_date = t.get("transaction_date", date.today())
                if isinstance(tx_date, str):
                    try:
                        tx_date = date.fromisoformat(tx_date)
                    except ValueError:
                        tx_date = date.today()

                obj = InternalTransaction(
                    id=str(uuid.uuid4()),
                    company_code=t.get("company_code", ""),
                    company_name=t.get("company_name", ""),
                    counterparty_code=t.get("counterparty_code", ""),
                    counterparty_name=t.get("counterparty_name", ""),
                    transaction_date=tx_date,
                    product_code=t.get("product_code", ""),
                    product_name=t.get("product_name", ""),
                    amount=Decimal(str(t.get("amount", 0) or 0)),
                    quantity=Decimal(str(t.get("quantity", 0) or 0)) if t.get("quantity") else None,
                    direction=t.get("direction", "buy"),
                    match_status=MatchStatus.UNMATCHED,
                    batch_id=batch_id,
                    source_system=t.get("source_system", "manual"),
                    raw_data=json.dumps(t, ensure_ascii=False, default=str),
                )
                objs.append(obj)
            self.session.bulk_save_objects(objs)
            self.session.commit()
            saved += len(objs)
        return saved

    def daily_fetch_all(self, target_date: date = None) -> Dict[str, Dict]:
        if target_date is None:
            target_date = date.today() - timedelta(days=1)
        subsidiaries = self.session.query(Subsidiary).filter_by(is_active=True).all()
        results = {}
        for sub in subsidiaries:
            code, count, message, elapsed = self._fetch_for_subsidiary(sub, target_date)
            is_success = count > 0
            is_skip = "跳过重复导入" in message
            is_empty = "0条记录" in message or "0条" in message or "未配置" in message
            if is_success:
                status = "成功"
            elif is_skip:
                status = "跳过"
            elif is_empty:
                status = "空数据"
            else:
                status = "失败"

            batch = FetchBatch(
                id=str(uuid.uuid4()),
                company_code=sub.code,
                company_name=sub.name,
                fetch_date=target_date,
                status=status,
                record_count=count,
                error_message=message if status != "成功" else None,
                duration_seconds=round(elapsed, 2),
            )
            self.session.add(batch)
            self.session.commit()

            results[code] = {
                "company_name": sub.name,
                "count": count,
                "status": status,
                "message": message,
                "elapsed": round(elapsed, 2),
            }
        return results


class TransactionMatcher:
    """内部交易双向匹配引擎 - 全场景差异工单生成"""

    def __init__(self, session: Session = None):
        self.session = session or SessionLocal()

    def _build_pair_key(self, buy_tx, sell_tx) -> str:
        pair = sorted([buy_tx.id, sell_tx.id])
        return f"{pair[0]}|{pair[1]}"

    def _work_order_exists(self, tx_id_1: str, tx_id_2: str = None) -> bool:
        q = self.session.query(DiscrepancyWorkOrder).filter(
            or_(
                DiscrepancyWorkOrder.transaction_id_buy == tx_id_1,
                DiscrepancyWorkOrder.transaction_id_sell == tx_id_1,
            )
        )
        if tx_id_2:
            q = q.filter(
                or_(
                    DiscrepancyWorkOrder.transaction_id_buy == tx_id_2,
                    DiscrepancyWorkOrder.transaction_id_sell == tx_id_2,
                )
            )
        return q.first() is not None

    def _create_discrepancy(self, buy_tx: InternalTransaction,
                            sell_tx: InternalTransaction = None,
                            discrepancy_type: str = "amount_mismatch"):
        if sell_tx:
            if self._work_order_exists(buy_tx.id, sell_tx.id):
                return None
        else:
            if self._work_order_exists(buy_tx.id):
                return None

        diff_amount = Decimal("0")
        responsible_code = buy_tx.company_code
        assigned_to = ""
        desc_parts = []

        if discrepancy_type == "amount_mismatch" and sell_tx:
            diff_amount = abs(buy_tx.amount - sell_tx.amount)
            responsible_code = buy_tx.company_code if buy_tx.amount > sell_tx.amount else sell_tx.company_code
            desc_parts.append(f"金额差异: 买方{buy_tx.company_code}金额{buy_tx.amount}, 卖方{sell_tx.company_code}金额{sell_tx.amount}, 差异{diff_amount}")

        elif discrepancy_type == "product_code_mismatch" and sell_tx:
            diff_amount = abs(buy_tx.amount - sell_tx.amount)
            responsible_code = buy_tx.company_code
            desc_parts.append(f"商品编码不一致: 买方{buy_tx.company_code}商品{buy_tx.product_code}, 卖方{sell_tx.company_code}商品{sell_tx.product_code}")

        elif discrepancy_type == "single_sided_buy":
            diff_amount = buy_tx.amount
            responsible_code = buy_tx.company_code
            desc_parts.append(f"单边流水(买方): {buy_tx.company_code}→{buy_tx.counterparty_code}, 商品{buy_tx.product_code}, 金额{buy_tx.amount}, 无卖方对应记录")

        elif discrepancy_type == "single_sided_sell":
            diff_amount = buy_tx.amount
            responsible_code = buy_tx.company_code
            desc_parts.append(f"单边流水(卖方): {buy_tx.company_code}→{buy_tx.counterparty_code}, 商品{buy_tx.product_code}, 金额{buy_tx.amount}, 无买方对应记录")

        if sell_tx:
            desc_parts.append(f"商品{buy_tx.product_code}")

        sub = self.session.query(Subsidiary).filter_by(code=responsible_code).first()
        if sub and sub.finance_staff:
            staff_list = [s.strip() for s in sub.finance_staff.split(",") if s.strip()]
            if staff_list:
                assigned_to = staff_list[0]

        wo = DiscrepancyWorkOrder(
            id=str(uuid.uuid4()),
            transaction_id_buy=buy_tx.id,
            transaction_id_sell=sell_tx.id if sell_tx else "",
            discrepancy_type=discrepancy_type,
            discrepancy_amount=diff_amount,
            discrepancy_description=", ".join(desc_parts),
            responsible_company_code=responsible_code,
            assigned_to=assigned_to,
            status=WorkOrderStatus.OPEN,
        )
        self.session.add(wo)
        return wo

    def _calculate_match_score(self, buy_tx: InternalTransaction, sell_tx: InternalTransaction) -> Tuple[float, str]:
        score = 0.0
        mismatch_type = "amount_mismatch"

        if buy_tx.counterparty_code == sell_tx.company_code:
            score += 0.4

        if buy_tx.product_code == sell_tx.product_code:
            score += 0.3
        else:
            score += 0.05
            mismatch_type = "product_code_mismatch"

        amount_diff = abs(buy_tx.amount - sell_tx.amount)
        if buy_tx.amount != 0:
            amount_ratio = float(amount_diff / buy_tx.amount)
            if amount_ratio <= DIFFERENCE_TOLERANCE:
                score += 0.3
            elif amount_ratio <= 0.05:
                score += 0.2
                mismatch_type = "amount_mismatch"
            elif amount_ratio <= 0.1:
                score += 0.1
                mismatch_type = "amount_mismatch"
            else:
                score += 0.0
                mismatch_type = "amount_mismatch"

        if buy_tx.transaction_date and sell_tx.transaction_date:
            date_diff = abs((buy_tx.transaction_date - sell_tx.transaction_date).days)
            if date_diff > 7:
                score -= 0.3
            elif date_diff > 3:
                score -= 0.1

        return max(score, 0), mismatch_type

    def match_batch(self, transaction_date: date = None, batch_id: str = None) -> Dict:
        query = self.session.query(InternalTransaction).filter(
            InternalTransaction.match_status == MatchStatus.UNMATCHED
        )
        if transaction_date:
            query = query.filter(InternalTransaction.transaction_date == transaction_date)
        if batch_id:
            query = query.filter(InternalTransaction.batch_id == batch_id)

        unmatched = query.all()
        logger.info(f"待匹配交易 {len(unmatched)} 笔")

        buy_map: Dict[str, List[InternalTransaction]] = {}
        sell_map: Dict[str, List[InternalTransaction]] = {}

        for t in unmatched:
            if t.direction == "buy":
                key = f"{t.counterparty_code}|{t.product_code}|{t.amount}"
                buy_map.setdefault(key, []).append(t)
            else:
                key = f"{t.company_code}|{t.product_code}|{t.amount}"
                sell_map.setdefault(key, []).append(t)

        matched_buy_ids = set()
        matched_sell_ids = set()
        matched_count = 0
        partial_count = 0
        discrepancy_count = 0

        for key, buy_list in buy_map.items():
            if key not in sell_map:
                continue
            sell_list = sell_map[key]

            for buy_tx in buy_list:
                if buy_tx.id in matched_buy_ids:
                    continue
                best_match = None
                best_score = 0
                best_mismatch_type = "amount_mismatch"
                for sell_tx in sell_list:
                    if sell_tx.id in matched_sell_ids:
                        continue
                    score, mismatch_type = self._calculate_match_score(buy_tx, sell_tx)
                    if score > best_score and score >= 0.7:
                        best_score = score
                        best_match = sell_tx
                        best_mismatch_type = mismatch_type

                if best_match:
                    if best_score >= 0.95:
                        buy_tx.match_status = MatchStatus.MATCHED
                        best_match.match_status = MatchStatus.MATCHED
                        buy_tx.matched_transaction_id = best_match.id
                        best_match.matched_transaction_id = buy_tx.id
                        buy_tx.match_score = best_score
                        best_match.match_score = best_score
                        matched_buy_ids.add(buy_tx.id)
                        matched_sell_ids.add(best_match.id)
                        matched_count += 1
                    else:
                        buy_tx.match_status = MatchStatus.PARTIAL
                        best_match.match_status = MatchStatus.PARTIAL
                        buy_tx.matched_transaction_id = best_match.id
                        best_match.matched_transaction_id = buy_tx.id
                        buy_tx.match_score = best_score
                        best_match.match_score = best_score
                        self._create_discrepancy(buy_tx, best_match, best_mismatch_type)
                        matched_buy_ids.add(buy_tx.id)
                        matched_sell_ids.add(best_match.id)
                        partial_count += 1
                        discrepancy_count += 1

        self.session.commit()

        remaining_buys = [t for t in unmatched if t.direction == "buy" and t.id not in matched_buy_ids]
        remaining_sells = [t for t in unmatched if t.direction == "sell" and t.id not in matched_sell_ids]

        single_sided_count = 0
        for tx in remaining_buys:
            if self._work_order_exists(tx.id):
                continue
            found_broad = self._find_broad_match(tx, remaining_sells, matched_sell_ids)
            if found_broad:
                score, mismatch_type = self._calculate_match_score(tx, found_broad)
                tx.match_status = MatchStatus.PARTIAL
                found_broad.match_status = MatchStatus.PARTIAL
                tx.matched_transaction_id = found_broad.id
                found_broad.matched_transaction_id = tx.id
                tx.match_score = score
                found_broad.match_score = score
                self._create_discrepancy(tx, found_broad, mismatch_type)
                matched_sell_ids.add(found_broad.id)
                partial_count += 1
                discrepancy_count += 1
            else:
                tx.match_status = MatchStatus.PARTIAL
                self._create_discrepancy(tx, None, "single_sided_buy")
                single_sided_count += 1
                discrepancy_count += 1

        for tx in remaining_sells:
            if tx.id in matched_sell_ids:
                continue
            if self._work_order_exists(tx.id):
                continue
            tx.match_status = MatchStatus.PARTIAL
            self._create_discrepancy(tx, None, "single_sided_sell")
            single_sided_count += 1
            discrepancy_count += 1

        self.session.commit()
        result = {
            "total_unmatched": len(unmatched),
            "matched": matched_count,
            "partial": partial_count,
            "single_sided": single_sided_count,
            "discrepancy_created": discrepancy_count,
        }
        logger.info(f"匹配完成: {result}")
        return result

    def _find_broad_match(self, buy_tx: InternalTransaction,
                          sells: List[InternalTransaction],
                          excluded_ids: set) -> Optional[InternalTransaction]:
        candidates = []
        for sell_tx in sells:
            if sell_tx.id in excluded_ids:
                continue
            if buy_tx.counterparty_code == sell_tx.company_code:
                score, _ = self._calculate_match_score(buy_tx, sell_tx)
                if score >= 0.4:
                    candidates.append((score, sell_tx))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]
        return None

    def match_concurrent(self, transaction_date: date = None) -> Dict:
        """高并发批量匹配 - 按公司对分组并行处理"""
        query = self.session.query(InternalTransaction).filter(
            InternalTransaction.match_status == MatchStatus.UNMATCHED
        )
        if transaction_date:
            query = query.filter(InternalTransaction.transaction_date == transaction_date)

        unmatched = query.all()
        if not unmatched:
            return {"total_unmatched": 0, "matched": 0, "partial": 0,
                    "single_sided": 0, "discrepancy_created": 0}

        pairs = set()
        for t in unmatched:
            if t.direction == "buy":
                pairs.add((t.company_code, t.counterparty_code))
            else:
                pairs.add((t.counterparty_code, t.company_code))

        total_matched = 0
        total_partial = 0
        total_discrepancy = 0
        total_single_sided = 0

        with ThreadPoolExecutor(max_workers=CONCURRENT_WORKERS) as executor:
            futures = {}
            for buyer_code, seller_code in pairs:
                future = executor.submit(
                    self._match_company_pair, buyer_code, seller_code, transaction_date
                )
                futures[future] = (buyer_code, seller_code)

            for future in as_completed(futures):
                pair = futures[future]
                try:
                    result = future.result()
                    total_matched += result["matched"]
                    total_partial += result["partial"]
                    total_discrepancy += result["discrepancy_created"]
                except Exception as e:
                    logger.error(f"匹配公司对 {pair} 失败: {e}")

        self.session.expire_all()
        remaining_query = self.session.query(InternalTransaction).filter(
            InternalTransaction.match_status == MatchStatus.UNMATCHED
        )
        if transaction_date:
            remaining_query = remaining_query.filter(
                InternalTransaction.transaction_date == transaction_date
            )
        remaining = remaining_query.all()

        for tx in remaining:
            if self._work_order_exists(tx.id):
                continue
            tx.match_status = MatchStatus.PARTIAL
            dtype = "single_sided_buy" if tx.direction == "buy" else "single_sided_sell"
            self._create_discrepancy(tx, None, dtype)
            total_single_sided += 1
            total_discrepancy += 1

        self.session.commit()

        return {
            "total_unmatched": len(unmatched),
            "matched": total_matched,
            "partial": total_partial,
            "single_sided": total_single_sided,
            "discrepancy_created": total_discrepancy,
        }

    def _match_company_pair(self, buyer_code: str, seller_code: str,
                            transaction_date: date = None) -> Dict:
        session = SessionLocal()
        try:
            query = session.query(InternalTransaction).filter(
                InternalTransaction.match_status == MatchStatus.UNMATCHED
            )
            if transaction_date:
                query = query.filter(InternalTransaction.transaction_date == transaction_date)

            buys = query.filter(
                InternalTransaction.company_code == buyer_code,
                InternalTransaction.counterparty_code == seller_code,
                InternalTransaction.direction == "buy"
            ).all()

            sells = query.filter(
                InternalTransaction.company_code == seller_code,
                InternalTransaction.counterparty_code == buyer_code,
                InternalTransaction.direction == "sell"
            ).all()

            matched = 0
            partial = 0
            discrepancy = 0
            matched_sell_ids = set()

            for buy_tx in buys:
                best_match = None
                best_score = 0
                best_mismatch_type = "amount_mismatch"
                for sell_tx in sells:
                    if sell_tx.id in matched_sell_ids:
                        continue
                    score, mismatch_type = self._calculate_match_score(buy_tx, sell_tx)
                    if score > best_score and score >= 0.7:
                        best_score = score
                        best_match = sell_tx
                        best_mismatch_type = mismatch_type

                if best_match:
                    if best_score >= 0.95:
                        buy_tx.match_status = MatchStatus.MATCHED
                        best_match.match_status = MatchStatus.MATCHED
                        buy_tx.matched_transaction_id = best_match.id
                        best_match.matched_transaction_id = buy_tx.id
                        buy_tx.match_score = best_score
                        best_match.match_score = best_score
                        matched_sell_ids.add(best_match.id)
                        matched += 1
                    else:
                        buy_tx.match_status = MatchStatus.PARTIAL
                        best_match.match_status = MatchStatus.PARTIAL
                        buy_tx.matched_transaction_id = best_match.id
                        best_match.matched_transaction_id = buy_tx.id
                        buy_tx.match_score = best_score
                        best_match.match_score = best_score

                        diff_amount = abs(buy_tx.amount - best_match.amount)
                        responsible_code = buy_tx.company_code if buy_tx.amount > best_match.amount else best_match.company_code
                        sub = session.query(Subsidiary).filter_by(code=responsible_code).first()
                        assigned_to = ""
                        if sub and sub.finance_staff:
                            staff_list = [s.strip() for s in sub.finance_staff.split(",") if s.strip()]
                            if staff_list:
                                assigned_to = staff_list[0]

                        existing = session.query(DiscrepancyWorkOrder).filter(
                            or_(
                                and_(
                                    DiscrepancyWorkOrder.transaction_id_buy == buy_tx.id,
                                    DiscrepancyWorkOrder.transaction_id_sell == best_match.id,
                                ),
                                and_(
                                    DiscrepancyWorkOrder.transaction_id_buy == best_match.id,
                                    DiscrepancyWorkOrder.transaction_id_sell == buy_tx.id,
                                ),
                            )
                        ).first()

                        if not existing:
                            desc = f"金额差异: 买方{buy_tx.company_code}金额{buy_tx.amount}, 卖方{best_match.company_code}金额{best_match.amount}, 差异{diff_amount}, 商品{buy_tx.product_code}"
                            if best_mismatch_type == "product_code_mismatch":
                                desc = f"商品编码不一致: 买方{buy_tx.company_code}商品{buy_tx.product_code}, 卖方{best_match.company_code}商品{best_match.product_code}"
                            wo = DiscrepancyWorkOrder(
                                id=str(uuid.uuid4()),
                                transaction_id_buy=buy_tx.id,
                                transaction_id_sell=best_match.id,
                                discrepancy_type=best_mismatch_type,
                                discrepancy_amount=diff_amount,
                                discrepancy_description=desc,
                                responsible_company_code=responsible_code,
                                assigned_to=assigned_to,
                                status=WorkOrderStatus.OPEN,
                            )
                            session.add(wo)
                            discrepancy += 1

                        matched_sell_ids.add(best_match.id)
                        partial += 1

            session.commit()
            return {"matched": matched, "partial": partial, "discrepancy_created": discrepancy}
        except Exception as e:
            session.rollback()
            raise
        finally:
            session.close()
