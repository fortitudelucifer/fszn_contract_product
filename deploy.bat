@echo off
:: 文件路径：fszn_contract_product/deploy.bat
chcp 65001

echo [1/3] 停止服务...
nssm stop fszn_contract_app

echo [2/3] 更新代码...
:: 复制当前目录下所有文件到 D:\fszn_erp\fszn_contract_product
:: 排除 uploads 文件夹（防止覆盖掉生产环境的合同文件！）
:: 排除 venv（生产环境有自己的 venv）
xcopy "%~dp0*" "D:\fszn_erp\fszn_contract_product\" /E /I /Y /EXCLUDE:exclude_list.txt

echo [3/3] 重启服务...
nssm start fszn_contract_app

echo 发布完成！
pause