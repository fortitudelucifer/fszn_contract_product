# 文件路径：fszn_contract_product/wsgi.py
import os
import sys

# 将当前目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fszn import create_app

app = create_app()

if __name__ == "__main__":
    from waitress import serve
    print("正在启动生产服务器 (Waitress)...")
    # 仅监听本地，配合 Tailscale 使用
    serve(app, host='127.0.0.1', port=8000)