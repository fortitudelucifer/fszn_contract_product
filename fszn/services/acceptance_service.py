# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Dict, Any

from flask_sqlalchemy import SQLAlchemy

from ..models import Acceptance, Contract


class AcceptanceService:
    """验收相关业务服务。

    当前主要职责：
        - 为单个合同提供验收统计信息：
          * 总验收次数
          * 通过的次数
          * 非通过/进行中的次数
    """

    def __init__(self, db: SQLAlchemy) -> None:
        self.db = db

    def get_summary_for_contract(self, contract: Contract) -> Dict[str, Any]:
        """获取指定合同的验收统计信息。

        返回的字典示例：
            {
                "total": 5,
                "passed": 3,
                "not_passed": 2,
            }
        """
        q = Acceptance.query.filter_by(contract_id=contract.id)
        records = q.all()

        total = len(records)
        passed = sum(1 for a in records if a.status == "通过")
        not_passed = total - passed

        return {
            "total": total,
            "passed": passed,
            "not_passed": not_passed,
        }
