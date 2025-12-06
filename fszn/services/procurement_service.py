# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Optional, Dict, Any

from flask_sqlalchemy import SQLAlchemy

from ..models import ProcurementItem, Contract  # 两个模型在现有 models.py 中已存在
from .notification_service import (
    NotificationService,
    DummyNotificationService,
    NotificationChannel,
)



class ProcurementService:
    """采购相关业务逻辑服务。

    说明：
        - 负责创建/更新采购条目
        - 在关键状态变更点调用通知服务（预留接口，当前使用 DummyNotificationService）
    """

    def __init__(
        self,
        db: SQLAlchemy,
        notification_service: Optional[NotificationService] = None,
    ) -> None:
        self.db = db
        # 如果外部未注入具体实现，则默认使用 Dummy 实现
        self.notification_service = notification_service or DummyNotificationService()

    def create_item(
        self,
        contract: Contract,
        data: Dict[str, Any],
        notify_target: Optional[str] = None,
        notify_channel: NotificationChannel = "email",
    ) -> ProcurementItem:
        """创建采购条目。
        :param contract: 关联的合同对象
        :param data: 前端传入的数据字典（物料名称/数量/单位/预计到货日期等）
        :param notify_target: 通知目标（邮箱 / 手机号 / 微信 openid 等），为空则不发送通知
        :param notify_channel: 通道类型，默认 email
        """
        item = ProcurementItem(
            contract_id=contract.id,
            item_name=data.get("item_name", "").strip(),
            quantity=data.get("quantity") or 0,
            unit=data.get("unit") or "",
            expected_date=data.get("expected_date"),
            remarks=data.get("remarks") or "",
        )
        self.db.session.add(item)
        self.db.session.commit()

        # 预留通知：新建采购条目时通知相关负责人（仅在显式提供目标时发送）
        self._notify_on_created(contract, item, notify_target, notify_channel)

        return item


    def update_status(
        self,
        item: ProcurementItem,
        new_status: str,
        notify_target: Optional[str] = None,
        notify_channel: NotificationChannel = "email",
    ) -> ProcurementItem:
        """更新采购条目的状态，并在关键状态变更时发送通知。
        :param item: 要更新的采购条目
        :param new_status: 新状态字符串
        :param notify_target: 通知目标（邮箱 / 手机号 / 微信 openid 等），为空则不发送通知
        :param notify_channel: 通道类型，默认 email
        """

        old_status = item.status
        item.status = new_status
        self.db.session.commit()

        self._notify_on_status_changed(
            item,
            old_status,
            new_status,
            notify_target,
            notify_channel,
        )

        return item


    # ------------------------------------------------------------------
    # 内部通知钩子方法（当前只调用 DummyNotificationService）
    # ------------------------------------------------------------------

    def _notify_on_created(
        self,
        contract: Contract,
        item: ProcurementItem,
        notify_target: Optional[str],
        notify_channel: NotificationChannel,
    ) -> None:
        """新建采购条目时的通知钩子。

        说明：
            - 当前阶段不再依赖 Contract 模型上的特定字段（如 responsible_email）
            - 仅当调用方显式传入 notify_target 时才发送通知
        """

        if not notify_target:
            # 未显式指定通知目标，则不发送任何通知
            return

        self.notification_service.send(
            channel=notify_channel,
            target=notify_target,
            template_code="PROCUREMENT_ITEM_CREATED",
            params={
                "contract_id": contract.id,
                "contract_name": getattr(contract, "name", ""),
                "item_name": item.item_name,
                "quantity": item.quantity,
                "unit": item.unit,
            },
        )

    def _notify_on_status_changed(
        self,
        item: ProcurementItem,
        old_status: str,
        new_status: str,
        notify_target: Optional[str],
        notify_channel: NotificationChannel,
    ) -> None:
        """采购状态变更时的通知钩子。

        说明：
            - 仍然只在关键状态（例如“已到货”）时触发
            - 通知目标由调用方显式传入，当前不依赖 Contract 上的特定字段
        """

        # 示例：只有从 “未采购/已下单/运输中” 变为 “已到货” 时才通知
        if new_status != "已到货":
            return

        if not notify_target:
            # 未显式指定通知目标，则不发送通知
            return

        contract = item.contract  # 利用 models.py 中的 relationship

        self.notification_service.send(
            channel=notify_channel,
            target=notify_target,
            template_code="PROCUREMENT_ITEM_ARRIVED",
            params={
                "contract_id": contract.id,
                "contract_name": getattr(contract, "name", ""),
                "item_name": item.item_name,
                "quantity": item.quantity,
                "unit": item.unit,
                "old_status": old_status,
                "new_status": new_status,
            },
        )
