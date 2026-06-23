@echo off
setlocal
cd /d "%~dp0"

python -c "import PyQt6" >nul 2>&1
python -c "import matplotlib" >nul 2>&1
python -c "import seaborn" >nul 2>&1

if errorlevel 1 (
    echo PyQt6 is not installed. Installing requirements...
    python -m pip install -r requirements.txt
) else (
    echo Requirements already available. Skipping install.
)

python app.py
pause
