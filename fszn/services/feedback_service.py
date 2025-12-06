# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Dict, Any

from flask_sqlalchemy import SQLAlchemy

from ..models import Feedback, Contract


class FeedbackService:
    """客户反馈相关业务服务。

    当前主要职责：
        - 为单个合同提供反馈统计信息：
          * 总反馈条数
          * 未解决条数
          * 已解决条数
    """

    def __init__(self, db: SQLAlchemy) -> None:
        self.db = db

    def get_summary_for_contract(self, contract: Contract) -> Dict[str, Any]:
        """获取指定合同的反馈统计信息。

        返回的字典示例：
            {
                "total": 4,
                "resolved": 3,
                "unresolved": 1,
            }
        """
        q = Feedback.query.filter_by(contract_id=contract.id)
        records = q.all()

        total = len(records)
        unresolved = sum(1 for fb in records if not getattr(fb, "is_resolved", False))
        resolved = total - unresolved

        return {
            "total": total,
            "resolved": resolved,
            "unresolved": unresolved,
        }
