@echo off
rem IP관리 portable EXE build (PyInstaller onefile, no console window)
rem Output: dist\IP관리.exe  -> copy to the shared network-drive folder
setlocal
cd /d "%~dp0"
rem Prefer a real install (the "python" on PATH may be the Microsoft Store alias stub)
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=py"

"%PY%" -m pip install --quiet --upgrade pyinstaller openpyxl || goto :error
"%PY%" -m unittest discover -s tests || goto :error

"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name IP관리 ^
  --paths . ^
  --exclude-module PIL --exclude-module numpy --exclude-module pandas --exclude-module lxml ^
  --exclude-module pytest --exclude-module pydoc --exclude-module doctest ^
  --exclude-module setuptools --exclude-module pkg_resources ^
  ipmgr\app.py || goto :error

echo.
echo BUILD OK: %cd%\dist\IP관리.exe
exit /b 0

:error
echo BUILD FAILED
exit /b 1
