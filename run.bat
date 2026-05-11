@echo off
chcp 65001 >nul 2>&1
title Trading Bot - Auto Setup & Run
color 0A

echo ==================================================
echo   TRADING BOT - AUTO SETUP ^& RUN
echo ==================================================
echo.

:: CEK PYTHON
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python tidak ditemukan!
    echo.
    echo Silakan download Python 3.10+ dari:
    echo   https://www.python.org/downloads/
    echo.
    echo PENTING: Centang "Add Python to PATH" saat install.
    echo.
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [OK] Python %PYVER% ditemukan.

:: CEK / BUAT VIRTUAL ENVIRONMENT
if not exist ".venv\Scripts\activate.bat" (
    echo.
    echo [SETUP] Membuat virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Gagal membuat virtual environment.
        pause
        exit /b 1
    )
    echo [OK] Virtual environment dibuat.
)

:: AKTIFKAN VENV
call .venv\Scripts\activate.bat
echo [OK] Virtual environment aktif.

:: INSTALL DEPENDENCIES
echo.
echo [SETUP] Menginstall dependencies (mungkin butuh beberapa menit)...
pip install -r requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 (
    echo.
    echo [ERROR] Gagal install dependencies.
    echo Coba manual: pip install -r requirements.txt
    pause
    exit /b 1
)
echo [OK] Semua dependencies terinstall.

:: CEK FILE .env
if not exist ".env" (
    echo.
    echo [WARN] File .env tidak ditemukan!
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo [AUTO] File .env dibuat dari .env.example.
        echo.
        echo !! Buka file .env dan isi API Key Bybit kamu !!
        echo.
        notepad .env
        echo.
        echo Tekan Enter setelah mengisi .env...
        pause >nul
    ) else (
        echo [ERROR] File .env.example tidak ditemukan.
        pause
        exit /b 1
    )
)
echo [OK] File .env ditemukan.

:: PILIH MODE
echo.
echo ==================================================
echo   PILIH MODE:
echo   [1] Jalankan BOT saja
echo   [2] Jalankan DASHBOARD
echo   [3] Jalankan KEDUANYA (bot + dashboard)
echo   [4] Validasi strategy.py
echo ==================================================
echo.
set /p CHOICE=Pilihan kamu (1/2/3/4): 

if "%CHOICE%"=="1" goto RUN_BOT
if "%CHOICE%"=="2" goto RUN_DASH
if "%CHOICE%"=="3" goto RUN_BOTH
if "%CHOICE%"=="4" goto RUN_CHECK
echo [ERROR] Pilihan tidak valid.
pause
exit /b 1

:RUN_BOT
echo.
echo [START] Menjalankan Trading Bot...
echo         Tekan Ctrl+C untuk berhenti.
echo.
python main.py
pause
exit /b 0

:RUN_DASH
echo.
echo [START] Menjalankan Dashboard...
echo         Buka browser: http://localhost:8501
echo.
streamlit run dashboard.py
pause
exit /b 0

:RUN_BOTH
echo.
echo [START] Menjalankan Bot di window terpisah...
start "Trading Bot" cmd /k "cd /d "%~dp0" && call .venv\Scripts\activate.bat && python main.py"
timeout /t 3 >nul
echo [START] Menjalankan Dashboard...
echo         Buka browser: http://localhost:8501
echo.
streamlit run dashboard.py
pause
exit /b 0

:RUN_CHECK
echo.
echo [START] Validasi strategy.py...
echo.
python preflight_check.py
echo.
pause
exit /b 0
