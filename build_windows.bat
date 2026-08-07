@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv .venv
    ) else (
        python -m venv .venv
    )
    if errorlevel 1 goto :error
)

call ".venv\Scripts\activate.bat"
python -m pip install --disable-pip-version-check -r requirements-build.txt
if errorlevel 1 goto :error
python -m PyInstaller --noconfirm --clean --onedir --noupx --windowed --name MacroPilot --collect-submodules pynput --collect-submodules mss --collect-submodules winrt main.py
if errorlevel 1 goto :error

echo.
echo Build complete: dist\MacroPilot\MacroPilot.exe
pause
exit /b 0

:error
echo.
echo Build failed. See the message above.
pause
exit /b 1
