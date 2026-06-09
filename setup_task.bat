@echo off
chcp 65001 >nul
echo ===== 创建选股系统定时任务 =====
echo 任务名称: A股智能选股系统
echo 执行时间: 每天 17:00
echo 执行命令: python main.py + python scripts/run_paper_trade.py
echo.
echo 正在创建任务...

schtasks /create /sc daily /tn "A股智能选股系统" /tr "cmd /c C:\Users\pc\AppData\Local\Python\pythoncore-3.14-64\python.exe d:\wwcode\stock\main.py && C:\Users\pc\AppData\Local\Python\pythoncore-3.14-64\python.exe d:\wwcode\stock\scripts\run_paper_trade.py" /st 17:00 /ru %USERNAME% /f

if %errorlevel% equ 0 (
    echo.
    echo ✓ 定时任务创建成功！
    echo 你可以在"任务计划程序"中查看和管理该任务。
) else (
    echo.
    echo ✗ 创建失败，请尝试以管理员身份运行此脚本。
)

pause
