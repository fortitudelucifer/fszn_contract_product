# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Protocol, Literal, Dict, Any
from flask import current_app
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr

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

# ========= 新增：通知服务工厂 =========

_notification_service_singleton: NotificationService | None = None


def get_notification_service() -> NotificationService:
    """根据配置返回一个全局可复用的通知服务实例。

    当前阶段：
    - 若配置为 'dummy' 或未配置，则使用 DummyNotificationService；
    - 未来可在此处扩展 email / sms / wechat 的真实实现。
    """
    global _notification_service_singleton
    if _notification_service_singleton is not None:
        return _notification_service_singleton

    backend = (current_app.config.get("NOTIFICATION_BACKEND") or "dummy").lower()

    # 暂时没有别的实现，先全部回退到 Dummy
    # 未来你新增 EmailNotificationService 等时，在这里做分支
    if backend == "dummy":
        _notification_service_singleton = DummyNotificationService()
    elif backend == "email":
        _notification_service_singleton = EmailNotificationService()
    else:
        _notification_service_singleton = DummyNotificationService()
    return _notification_service_singleton


class EmailNotificationService:
    """基于 SMTP 的邮箱通知实现。

    说明：
    - 使用 config 中的 MAIL_* 配置；
    - 仅在 channel == 'email' 时真正发邮件，
      其它通道暂时仍打印日志（避免调用方报错）。
    """

    def __init__(self) -> None:
        cfg = current_app.config
        self.server = cfg.get("MAIL_SERVER")
        self.port = cfg.get("MAIL_PORT", 587)
        self.use_tls = cfg.get("MAIL_USE_TLS", True)
        self.username = cfg.get("MAIL_USERNAME")
        self.password = cfg.get("MAIL_PASSWORD")
        self.default_sender = cfg.get("MAIL_DEFAULT_SENDER") or self.username

    def send(
        self,
        channel: NotificationChannel,
        target: str,
        template_code: str,
        params: Dict[str, Any] | None = None,
    ) -> None:
        params = params or {}

        if channel != "email":
            # 暂时对非 email 通道不做真实发送，避免业务出错
            print(
                f"[Notification:EmailService] non-email channel={channel}, "
                f"target={target}, template={template_code}, params={params}"
            )
            return

        # 简单示例：用 template_code 拼一个主题，message 作为正文
        subject = f"[{template_code}] 合同通知"
        message = params.get("message") or ""

        msg = MIMEText(message, "plain", "utf-8")
        msg["From"] = formataddr(("系统通知", self.default_sender))
        msg["To"] = target
        msg["Subject"] = subject

        with smtplib.SMTP(self.server, self.port) as s:
            if self.use_tls:
                s.starttls()
            if self.username and self.password:
                s.login(self.username, self.password)
            s.sendmail(self.default_sender, [target], msg.as_string())

        print(
            f"[Notification:EmailService] sent email to {target}, "
            f"template={template_code}, params={params}"
        )
