@echo off
setlocal
if exist "%LOCALAPPDATA%\agy\bin\agy.exe" (
    "%LOCALAPPDATA%\agy\bin\agy.exe"
) else (
    agy
)
pause
