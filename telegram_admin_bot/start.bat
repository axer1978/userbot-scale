@echo off
REM   start.bat            run the default instance
REM   start.bat NAME       run a separate instance for another Telegram account
REM                        (created on first use; its panel asks you to sign in)
REM   start.bat all        open one window per instance, all at once
REM   start.bat list       show the instances that exist
REM
REM Each instance has its own login, conversations, settings and panel port,
REM kept under instances\NAME\. One instance = one Telegram account.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating the Python environment ^(first run only^)...
    python -m venv .venv || goto :fail
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
)

if /I "%~1"=="all"  goto :all
if /I "%~1"=="list" goto :list
if "%~1"==""        goto :default

".venv\Scripts\python.exe" main.py --instance "%~1"
goto :end

:default
".venv\Scripts\python.exe" main.py
goto :end

:list
".venv\Scripts\python.exe" main.py --list
goto :end

:all
REM The default instance only counts once it has been signed in.
if exist ".env" start "Telegram assistant" cmd /c ""%~f0""
if exist "instances\" (
    for /d %%d in ("instances\*") do (
        start "Telegram assistant - %%~nxd" cmd /c ""%~f0" "%%~nxd""
    )
)
exit /b 0

:fail
echo.
echo Setup failed. Make sure Python 3.11+ is installed and on PATH.
:end
pause
