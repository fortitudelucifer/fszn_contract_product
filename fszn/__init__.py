# -*- coding: utf-8 -*-
import os
from flask import Flask, render_template, session, redirect, url_for, request, abort

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def create_app():
    app = Flask(__name__)

    @app.before_request
    def restrict_customer_access():
        # 1. 放行静态资源（CSS/JS），否则页面会乱码
        if request.endpoint and request.endpoint.startswith('static'):
            return

        # 2. 获取当前登录用户 ID
        user_id = session.get('user_id')
        
        # 如果用户已登录，进行权限检查
        if user_id:
            # 局部引用 User 模型，避免循环导入
            from .models import User
            user = User.query.get(user_id)
            
            # 检查是否为受限角色 'customer'
            if user and user.role == 'customer':
                # 定义允许访问的端点白名单 (主要是 auth 模块的登录、注册、注销)
                # 'auth.logout' 必须允许，否则用户无法退出切换账号
                allowed_endpoints = ['auth.login', 'auth.register', 'auth.logout']
                
                # 如果当前请求的端点不在白名单中，则拒绝访问
                if request.endpoint not in allowed_endpoints:
                    # 方法 A: 直接返回 403 禁止访问错误
                    # return "您的账号权限受限，仅允许注册和登录。", 403
                    
                    # 方法 B (推荐): 渲染一个友好的拒绝页面
                    # return render_template('403_customer.html'), 403
                    
                    # 方法 C (简单): 闪现消息并重定向回登录页(或注销)
                    # from flask import flash
                    # flash('普通用户无权访问系统功能，请联系管理员。')
                    # return redirect(url_for('auth.logout'))
                    
                    # 这里使用最直接的拒绝方式：
                    return "<h1>403 Forbidden</h1><p>游客角色无权访问此系统。请<a href='/auth/logout'>退出</a>后使用员工账号登录。</p>", 403

    app.config.from_object('config.Config')

    
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))


    app.config['SQLALCHEMY_DATABASE_URI'] ='mssql+pyodbc://fszn_user:fszn123!@localhost/fszn_db_product?driver=ODBC+Driver+17+for+SQL+Server&TrustServerCertificate=yes'
    app.config['SECRET_KEY'] = 'dev' 
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # 文件上传相关配置
    app.config['UPLOAD_FOLDER'] = os.path.join(BASE_DIR, 'uploads')
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB，可调

    db.init_app(app)

    # 登录/注册
    from .auth import auth_bp
    app.register_blueprint(auth_bp, url_prefix='/auth')

    # 项目/合同
    from .contracts import contracts_bp
    app.register_blueprint(contracts_bp)

    # 部门 & 人员
    from .org import org_bp
    app.register_blueprint(org_bp, url_prefix='/org')

    # 操作日志
    from .logs import logs_bp
    app.register_blueprint(logs_bp, url_prefix='/logs')


    @app.route('/')
    def home():
        from .models import User
        user = None
        user_id = session.get('user_id')
        if user_id:
            user = User.query.get(user_id)
        return render_template('home.html', user=user)

    @app.context_processor
    def inject_common():
        def human_filesize(num_bytes):
            if num_bytes is None:
                return ''
            try:
                n = int(num_bytes)
            except (TypeError, ValueError):
                return str(num_bytes)

            if n < 1024:
                return f"{n} B"
            kb = n / 1024
            if kb < 1024:
                return f"{kb:.1f} KB"
            mb = kb / 1024
            return f"{mb:.1f} MB"

        return dict(
            config=app.config,
            human_filesize=human_filesize,
        )

       
    # 确保上传目录存在
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    return app
