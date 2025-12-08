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

    # == 如果用 email 作为通知渠道，配置 SMTP ==
    MAIL_SERVER = "smtp.example.com"
    MAIL_PORT = 587
    MAIL_USE_TLS = True
    MAIL_USERNAME = "1284634061@qq.com"
    MAIL_PASSWORD = "your_password"
    MAIL_DEFAULT_SENDER = "noreply@example.com"