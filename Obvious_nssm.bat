@echo off
echo 【此处填写你注册的NSSM服务名】_nssm_service_manager
echo.
echo 1. start
echo 2. stop
echo 3. restart
echo 4. status
echo 5. log
echo 6. exit
echo.
set /p choice="please select an operation (1-6): "

if "%choice%"=="1" (
    nssm start 【【此处填写你注册的NSSM服务名】】
    pause
) else if "%choice%"=="2" (
    nssm stop 【此处填写你注册的NSSM服务名】
    pause
) else if "%choice%"=="3" (
    nssm restart 【此处填写你注册的NSSM服务名】
    pause
) else if "%choice%"=="4" (
    nssm status 【此处填写你注册的NSSM服务名】
    pause
) else if "%choice%"=="5" (
    notepad "【此处填写你的日志路径】"
) else if "%choice%"=="6" (
    exit
) else (
    echo invailid choice
    pause
)
