# fszn/services/notification_service.py

from __future__ import annotations

from typing import Any, Dict, Protocol, Literal
from email.mime.text import MIMEText
from email.utils import formataddr
import smtplib,time, hmac, hashlib, base64, urllib.parse, argparse
from urllib.parse import urlencode
import requests
from flask import current_app


# 通道枚举：email / sms / wechat（个人微信）/ wechat_corp（企业微信）/ ding
NotificationChannel = Literal["email", "sms", "wechat", "wechat_corp", "ding"]



class NotificationService(Protocol):
    """通知服务接口协议：业务只依赖这一层"""

    def send(
        self,
        channel: NotificationChannel,
        target: str,
        template_code: str,
        params: Dict[str, Any] | None = None,
    ) -> None:
        ...


class DummyNotificationService:
    """占位实现：仅打印日志，开发/测试环境用"""

    def send(
        self,
        channel: NotificationChannel,
        target: str,
        template_code: str,
        params: Dict[str, Any] | None = None,
    ) -> None:
        print(
            f"[Notification:Dummy] channel={channel}, target={target}, "
            f"template={template_code}, params={params}"
        )


class EmailNotificationService:
    """基于 SMTP 的邮件通知实现（目前只处理 channel='email'）"""

    def __init__(
        self,
        server: str,
        port: int,
        use_tls: bool,
        username: str | None,
        password: str | None,
        default_sender: str,
    ) -> None:
        self.server = server
        self.port = port
        self.use_tls = use_tls
        self.username = username
        self.password = password
        self.default_sender = default_sender

    def send(
        self,
        channel: NotificationChannel,
        target: str,
        template_code: str,
        params: Dict[str, Any] | None = None,
    ) -> None:
        params = params or {}

        # 目前只处理 email 通道，其他通道直接打印日志
        if channel != "email":
            print(
                f"[Notification:EmailService] non-email channel={channel}, "
                f"target={target}, template={template_code}, params={params}"
            )
            return

        if not target:
            print(
                f"[Notification:EmailService] empty target, "
                f"template={template_code}, params={params}"
            )
            return

        # subject 可以让调用方通过 params 传；没传就用 template_code 兜底
        subject = params.get("subject") or f"[{template_code}] 合同通知"
        message = params.get("message") or ""

        msg = MIMEText(message, "plain", "utf-8")
        msg["From"] = formataddr(("系统通知", self.default_sender))
        msg["To"] = target
        msg["Subject"] = subject

        with smtplib.SMTP(self.server, self.port) as smtp:
            if self.use_tls:
                smtp.starttls()
            if self.username and self.password:
                smtp.login(self.username, self.password)
            smtp.sendmail(self.default_sender, [target], msg.as_string())


class DingTalkRobotNotificationService:
    """钉钉群自定义机器人通知实现（channel='ding'）"""

    def __init__(self, webhook: str, secret: str | None = None) -> None:
        self.webhook = webhook
        self.secret = secret or ""

    def _build_signed_url(self) -> str:
        """如果配置了 secret，则按钉钉规范做 timestamp + sign"""
        if not self.secret:
            return self.webhook

        timestamp = str(int(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self.secret}".encode("utf-8")
        h = hmac.new(self.secret.encode("utf-8"), string_to_sign, hashlib.sha256)
        sign = base64.b64encode(h.digest()).decode("utf-8")
        query = urlencode({"timestamp": timestamp, "sign": sign})
        if "?" in self.webhook:
            return f"{self.webhook}&{query}"
        return f"{self.webhook}?{query}"

    def send(
        self,
        channel: NotificationChannel,
        target: str,
        template_code: str,
        params: Dict[str, Any] | None = None,
    ) -> None:
        params = params or {}

        if channel != "ding":
            # 多余的调用直接忽略，不抛异常
            print(
                f"[Notification:DingTalk] skip non-ding channel={channel}, "
                f"template={template_code}, params={params}"
            )
            return

        # ===== 从 params 里取字段 =====
        event_label = (
            params.get("event_label")
            or params.get("event_code")
            or template_code
        )
        company = params.get("company_name") or ""
        contract_number = params.get("contract_number") or ""
        contract_name = params.get("contract_name") or ""
        operator_name = params.get("operator_name") or ""
        message = params.get("message") or ""
        contract_url = params.get("contract_url") or ""

        # ===== 构建 Markdown 内容 =====
        md_lines = [
            f"### 合同事件：**{event_label}**",
        ]

        if company:
            md_lines.append(f"> 所属公司：{company}")

        if contract_number:
            md_lines.append(f"> 合同编号：{contract_number}")

        if contract_name:
            md_lines.append(f"> 合同名称：{contract_name}")

        if operator_name:
            md_lines.append(f"> 操作人：{operator_name}")

        if message:
            md_lines.append(f"> 说明：{message}")

        if contract_url:
            md_lines.append(f"[点击查看合同详情]({contract_url})")

        content = "\n".join(md_lines)

        # ===== @手机号码（如果 target 是手机号） =====
        at_mobiles = []
        if target and target.strip().isdigit():
            at_mobiles = [target.strip()]

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"合同事件：{event_label}",
                "text": content,
            },
            "at": {
                "atMobiles": at_mobiles,
                "isAtAll": False,
            },
        }

        url = self._build_signed_url()
        try:
            resp = requests.post(url, json=payload, timeout=5)
            if resp.status_code != 200:
                print(
                    f"[Notification:DingTalk] http_error status={resp.status_code}, "
                    f"body={resp.text}"
                )
        except Exception as exc:
            print(
                f"[Notification:DingTalk] send_error exc={exc!r}, "
                f"payload={payload}"
            )


class WeComRobotNotificationService:
    """企业微信群自定义机器人通知实现（channel='wechat_corp'）"""

    def __init__(self, webhook: str) -> None:
        self.webhook = webhook

    def send(
        self,
        channel: NotificationChannel,
        target: str,
        template_code: str,
        params: Dict[str, Any] | None = None,
    ) -> None:
        params = params or {}

        if channel != "wechat_corp":
            print(
                f"[Notification:WeCom] skip non-wechat_corp channel={channel}, "
                f"template={template_code}, params={params}"
            )
            return

        contract_number = params.get("contract_number") or ""
        contract_name = params.get("contract_name") or ""
        event_code = params.get("event_code") or template_code
        operator_name = params.get("operator_name") or ""
        extra_message = params.get("message") or ""
        contract_url = params.get("contract_url") or ""

        # 用 markdown 格式，让企业微信里看起来更友好
        lines = [f"**合同事件：{event_code}**"]
        if contract_number:
            lines.append(f"> 合同编号：{contract_number}")
        if contract_name:
            lines.append(f"> 合同名称：{contract_name}")
        if operator_name:
            lines.append(f"> 操作人：{operator_name}")
        if extra_message:
            lines.append(f"> 说明：{extra_message}")
        if contract_url:
            lines.append(f"[点击查看合同]({contract_url})")

        content = "\n".join(lines)

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": content,
            },
        }

        try:
            resp = requests.post(self.webhook, json=payload, timeout=5)
            if resp.status_code != 200:
                print(
                    f"[Notification:WeCom] http_error status={resp.status_code}, "
                    f"body={resp.text}"
                )
        except Exception as exc:
            print(
                f"[Notification:WeCom] send_error exc={exc!r}, "
                f"payload={payload}"
            )


class RoutedNotificationService:
    """
    根据 channel 路由到具体实现的“中间层”。

    - email：优先走 email 实现，没配则走默认 backend（一般是 Dummy）
    - sms/wechat/ding：暂时都回退到默认 backend，未来可以各自挂真实实现
    """

    def __init__(
        self,
        default_backend: NotificationService,
        email: NotificationService | None = None,
        ding: NotificationService | None = None,
        wechat: NotificationService | None = None,
        sms: NotificationService | None = None,
    ) -> None:
        self.default_backend = default_backend
        self.email = email
        self.ding = ding
        self.wechat = wechat
        self.sms = sms

    def send(
        self,
        channel: NotificationChannel,
        target: str,
        template_code: str,
        params: Dict[str, Any] | None = None,
    ) -> None:
        params = params or {}

        if channel == "email":
            backend = self.email or self.default_backend
        elif channel == "sms":
            backend = self.sms or self.default_backend
        elif channel == "wechat_corp":
            # 企业微信机器人
            backend = self.wechat or self.default_backend
        elif channel == "wechat":
            # 个人微信目前没有真实实现，先走默认 backend（一般是 Dummy）
            backend = self.default_backend
        elif channel == "ding":
            backend = self.ding or self.default_backend
        else:
            backend = self.default_backend

        try:
            backend.send(channel, target, template_code, params)
        except Exception as exc:  # 不让业务崩，兜底打印日志
            print(
                f"[Notification:Routed] error channel={channel}, "
                f"target={target}, template={template_code}, "
                f"params={params}, exc={exc!r}"
            )


# ====== 工厂 & 单例 ======

_notification_service_singleton: NotificationService | None = None


def _build_notification_service_from_config() -> NotificationService:
    """根据 Flask 配置构造一个 NotificationService 实例。"""
    cfg = current_app.config
    backend = (cfg.get("NOTIFICATION_BACKEND") or "dummy").lower()

    # 默认后端：Dummy
    dummy = DummyNotificationService()

    # ========== 邮件后端 ==========
    email_service: NotificationService | None = None
    if backend == "email":
        server = cfg.get("MAIL_SERVER")
        port = int(cfg.get("MAIL_PORT", 25))
        use_tls = bool(cfg.get("MAIL_USE_TLS", False))
        username = cfg.get("MAIL_USERNAME")
        password = cfg.get("MAIL_PASSWORD")
        default_sender = cfg.get("MAIL_DEFAULT_SENDER") or username

        if not server or not default_sender:
            print(
                "[Notification] NOTIFICATION_BACKEND=email 但 SMTP 配置不完整，"
                "回退到 DummyNotificationService。"
            )
        else:
            email_service = EmailNotificationService(
                server=server,
                port=port,
                use_tls=use_tls,
                username=username,
                password=password,
                default_sender=default_sender,
            )

    # ========== 钉钉机器人 ==========
    ding_service: NotificationService | None = None
    ding_webhook = cfg.get("DINGTALK_WEBHOOK_URL")
    if ding_webhook:
        ding_secret = cfg.get("DINGTALK_SECRET")
        ding_service = DingTalkRobotNotificationService(
            webhook=ding_webhook,
            secret=ding_secret,
        )

    # ========== 企业微信群机器人 ==========
    wecom_service: NotificationService | None = None
    wecom_webhook = cfg.get("WECOM_WEBHOOK_URL")
    if wecom_webhook:
        wecom_service = WeComRobotNotificationService(
            webhook=wecom_webhook,
        )

    # 路由器：默认 backend 仍然是 dummy
    return RoutedNotificationService(
        default_backend=dummy,
        email=email_service,
        ding=ding_service,
        wechat=wecom_service,
        # sms 暂时没有真实实现，就不传，保持走 dummy
    )


def get_notification_service() -> NotificationService:
    """获取全局通知服务实例（惰性构造 + 单例）"""
    global _notification_service_singleton
    if _notification_service_singleton is None:
        _notification_service_singleton = _build_notification_service_from_config()
    return _notification_service_singleton
