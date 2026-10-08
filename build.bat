@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul || (echo Python launcher "py" not found. Install Python 3.10+ from python.org & exit /b 1)

if not exist .venv (
    py -3 -m venv .venv || exit /b 1
)
call .venv\Scripts\activate.bat

python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller || exit /b 1

python make_icon.py || exit /b 1

pyinstaller --noconfirm --clean --windowed --onedir ^
  --name DDCPanel --icon icon.ico ddcpanel.py || exit /b 1

powershell -NoProfile -Command "Compress-Archive -Path 'dist\DDCPanel' -DestinationPath 'dist\DDCPanel.zip' -Force"

echo.
echo Built: dist\DDCPanel\DDCPanel.exe
echo Zip:   dist\DDCPanel.zip