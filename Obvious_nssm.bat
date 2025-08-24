@echo off
:menu
cls
echo ObviousRec_nssm_service_manager
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
    nssm start ObviousRec
    pause
    goto menu
) else if "%choice%"=="2" (
    nssm stop ObviousRec
    pause
    goto menu
) else if "%choice%"=="3" (
    nssm restart ObviousRec
    pause
    goto menu
) else if "%choice%"=="4" (
    nssm status ObviousRec
    pause
    goto menu
) else if "%choice%"=="5" (
    notepad "E:\EEW\service.log"
    goto menu
) else if "%choice%"=="6" (
    exit
) else (
    echo invalid choice
    pause
    goto menu
)