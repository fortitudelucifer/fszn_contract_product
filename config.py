import os

class Config:
    # 用于 session、Flash 等，正式环境记得改成随机复杂的
    SECRET_KEY = os.environ.get('FSZN_SECRET_KEY', 'a-super-long-and-random-string-that-no-one-can-guess-123!@#')

    # SQL Server 连接字符串（使用 pyodbc + SQLAlchemy）
    # 注意把用户名、密码、数据库名改成你自己的
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        'FSZN_DATABASE_URI',
        'mssql+pyodbc://fszn_user:fszn123!@localhost/fszn_db_product'
        '?driver=ODBC+Driver+17+for+SQL+Server&TrustServerCertificate=yes'
    )

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # 通知后端：dummy / email （未来可以扩展 sms / wechat）
    NOTIFICATION_BACKEND = "dummy"

    # ===== 钉钉群机器人配置 =====
    # 钉钉机器人 webhook（完整 URL）
    DINGTALK_WEBHOOK_URL = "https://oapi.dingtalk.com/robot/send?access_token=a5592eead55e99fb70ed18bda20dd45bad794240110188f7b69fa80cfed27315"
    # 如果你在钉钉机器人里启用了“加签”，在这里填入 secret；没启用就留空
    DINGTALK_SECRET = "SECbd13bbc0a8fa94b4bb30b09b1822f5dcc62ad1610896c2446095f53007297bc0"

    # ===== 企业微信（WeCom）群机器人配置 =====
    # 企业微信群机器人 webhook（完整 URL）
    WECOM_WEBHOOK_URL = ""

    # == 如果用 email 作为通知渠道，配置 SMTP ==
    MAIL_SERVER = "smtp.example.com"
    MAIL_PORT = 587
    MAIL_USE_TLS = True
    MAIL_USERNAME = "1284634061@qq.com"
    MAIL_PASSWORD = "your_password"
    MAIL_DEFAULT_SENDER = "noreply@example.com"