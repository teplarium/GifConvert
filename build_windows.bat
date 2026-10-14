@echo off
rem ============================================================
rem  Build GifConvert.exe on this Windows PC.
rem    build_windows.bat        -> 64-bit  (dist\win64\GifConvert.exe)
rem    build_windows.bat 32     -> 32-bit  (dist\win32\GifConvert.exe)
rem  Needs Python 3.9+ from python.org of the same bitness
rem  (for 32-bit: "Windows installer (32-bit)").
rem ============================================================
setlocal
cd /d "%~dp0"

set "ARCH=%~1"
if "%ARCH%"=="" set "ARCH=64"
if not "%ARCH%"=="32" if not "%ARCH%"=="64" (
    echo Usage: build_windows.bat [32^|64]
    exit /b 1
)

set "PY=py -3-%ARCH%"
where py >nul 2>nul || set "PY=python"

echo [1/4] Checking Python...
%PY% -c "import struct, sys, tkinter; b=struct.calcsize('P')*8; print('      Python', sys.version.split()[0], b, 'bit'); sys.exit(0 if str(b)=='%ARCH%' else 42)"
set "RC=%errorlevel%"
if "%RC%"=="42" (
    echo.
    echo Found Python is not %ARCH%-bit. Install %ARCH%-bit Python from python.org.
    goto :fail
)
if not "%RC%"=="0" (
    echo.
    echo Python %ARCH%-bit with tkinter not found.
    echo Install it from https://www.python.org/downloads/windows/
    goto :fail
)

echo [2/4] Preparing the build environment...
set "VENV=.venv-build-%ARCH%"
rem a half-created environment from an interrupted run is recreated
if exist "%VENV%\Scripts\python.exe" (
    "%VENV%\Scripts\python.exe" -m pip --version >nul 2>nul || rmdir /s /q "%VENV%"
)
if not exist "%VENV%\Scripts\python.exe" (
    %PY% -m venv "%VENV%" || goto :fail
)
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip >nul

echo [3/4] Installing PyInstaller, ffmpeg, Pillow...
"%VENV%\Scripts\python.exe" -m pip install -r requirements-build.txt || goto :fail

echo [4/4] Building...
"%VENV%\Scripts\python.exe" -m PyInstaller --noconfirm --clean --distpath "dist\win%ARCH%" GifConvert.spec || goto :fail

echo.
echo ============================================================
echo  Done: dist\win%ARCH%\GifConvert.exe
echo ============================================================
pause
exit /b 0

:fail
echo.
echo Build failed. Scroll up to see the error.
pause
exit /b 1
