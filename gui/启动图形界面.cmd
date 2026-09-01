@echo off
setlocal EnableExtensions
set "PYTHON_EXE="

if defined SCOOP if exist "%SCOOP%\apps\python\current\python.exe" set "PYTHON_EXE=%SCOOP%\apps\python\current\python.exe"
if not defined PYTHON_EXE if exist "D:\Scoop\apps\python\current\python.exe" set "PYTHON_EXE=D:\Scoop\apps\python\current\python.exe"
if not defined PYTHON_EXE if exist "%USERPROFILE%\scoop\apps\python\current\python.exe" set "PYTHON_EXE=%USERPROFILE%\scoop\apps\python\current\python.exe"
if not defined PYTHON_EXE for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"

if not defined PYTHON_EXE (
    echo [ERROR] Python was not found.
    echo Install Python or edit PYTHON_EXE in this launcher.
    pause
    exit /b 1
)

"%PYTHON_EXE%" "%~dp0FileUniquefyGUI.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
