# -*- coding: utf-8 -*-
from fszn import create_app, db
from sqlalchemy import text

app = create_app()

with app.app_context():
    print("正在检查数据库 companies 表的约束...")

    # 1. 查找并删除唯一约束 (Unique Constraint)
    # SQL Server 系统表查询：查找 companies 表上的 UQ 类型约束
    sql_find_constraint = """
    SELECT name
    FROM sys.key_constraints
    WHERE type = 'UQ' AND parent_object_id = OBJECT_ID('companies');
    """
    
    constraints = db.session.execute(text(sql_find_constraint)).fetchall()
    
    if constraints:
        for row in constraints:
            constraint_name = row[0]
            print(f"发现唯一约束: {constraint_name}，正在删除...")
            try:
                # 删除约束
                db.session.execute(text(f"ALTER TABLE companies DROP CONSTRAINT {constraint_name}"))
                print(" -> 删除成功")
            except Exception as e:
                print(f" -> 删除失败: {e}")
    else:
        print("未发现唯一约束 (Key Constraint)。")

    # 2. 查找并删除唯一索引 (Unique Index)
    # 有时候 unique=True 会创建为唯一索引而不是约束
    sql_find_index = """
    SELECT name 
    FROM sys.indexes 
    WHERE object_id = OBJECT_ID('companies') 
    AND is_unique = 1 
    AND is_primary_key = 0; -- 排除主键
    """
    
    indexes = db.session.execute(text(sql_find_index)).fetchall()
    
    if indexes:
        for row in indexes:
            index_name = row[0]
            print(f"发现唯一索引: {index_name}，正在删除...")
            try:
                # 删除索引
                db.session.execute(text(f"DROP INDEX {index_name} ON companies"))
                print(" -> 删除成功")
            except Exception as e:
                print(f" -> 删除失败: {e}")
    else:
        print("未发现额外唯一索引。")

    db.session.commit()
    print("------------------------------------------------")
    print("修复完成！现在您可以随意修改公司名称了。")