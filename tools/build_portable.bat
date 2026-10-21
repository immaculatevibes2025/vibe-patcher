@echo off
rem Builds the portable Vibe Patcher (folder with Vibe Patcher.exe) into ..\dist
rem Needs:  pip install pyinstaller requests pillow beautifulsoup4 cryptography
cd /d "%~dp0.."
rem bundle UnRAR.exe (freely redistributable - WinRAR licence 3a) so every RAR opens
if not exist "assets\tools" mkdir "assets\tools"
if exist "%ProgramFiles%\WinRAR\UnRAR.exe" (
  copy /y "%ProgramFiles%\WinRAR\UnRAR.exe" "assets\tools\UnRAR.exe" >nul
  copy /y "%ProgramFiles%\WinRAR\License.txt" "assets\tools\UnRAR-License.txt" >nul
)
pyinstaller --noconfirm --clean --windowed --name "Vibe Patcher" ^
  --icon "%CD%\assets\icon.ico" --add-data "%CD%\assets;assets" ^
  --distpath "%CD%\..\dist" --workpath "%TEMP%\vibepatcher_build" --specpath "%TEMP%\vibepatcher_build" ^
  app.py
echo.
echo Done: the portable app is in "%CD%\..\dist\Vibe Patcher"
pause
