# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Protocol, Literal, Dict, Any


NotificationChannel = Literal["email", "sms", "wechat"]


class NotificationService(Protocol):
    """通知服务接口协议。

    说明：
        - 目前只是一个抽象协议，后续可以实现多个具体类：
          EmailNotificationService / SmsNotificationService / WechatNotificationService 等。
        - 在 v2.0 初期，我们可以只做一个“打印日志”的假实现，便于调试调用链。
    """

    def send(
        self,
        channel: NotificationChannel,
        target: str,
        template_code: str,
        params: Dict[str, Any] | None = None,
    ) -> None:
        """发送通知的统一入口。

        :param channel: 通道类型，例如 "email" / "sms" / "wechat"
        :param target: 目标地址，例如邮箱、手机号、微信 openid 等
        :param template_code: 模板编码，如 "PROCUREMENT_ORDER_CREATED"
        :param params: 模板参数字典，用于渲染模板内容
        """
        ...


class DummyNotificationService:
    """简单的占位实现：目前只是在服务器日志/控制台打印，方便调试调用流程。"""

    def send(
        self,
        channel: NotificationChannel,
        target: str,
        template_code: str,
        params: Dict[str, Any] | None = None,
    ) -> None:
        # TODO: 将来这里可以替换为真正的发送逻辑（邮件/短信/微信）
        print(
            f"[Notification] channel={channel}, target={target}, "
            f"template={template_code}, params={params}"
        )
