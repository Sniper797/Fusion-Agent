@echo off
rem Copies the FusionAgent add-in into Fusion's add-ins folder for this user.
set "DEST=%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\FusionAgent"
xcopy /E /I /Y /Q "%~dp0FusionAgent" "%DEST%" >nul
if errorlevel 1 (
    echo Copy failed.
    pause
    exit /b 1
)
echo FusionAgent installed in:
echo   %DEST%
echo.
echo Next: in Fusion open Utilities ^> Add-Ins (Shift+S), find FusionAgent in the list and run it.
echo run_job.py is NOT added to Fusion: it runs from a terminal in this folder.
echo Then, from this folder:  python run_job.py ping
pause
