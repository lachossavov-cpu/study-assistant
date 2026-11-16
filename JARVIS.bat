@echo off
rem Стартира таблото и го отваря като отделен прозорец без адресна лента.
cd /d "%~dp0"

rem 1. сървърът (мълчаливо, в отделен минимизиран прозорец)
start "JARVIS server" /min python server\app.py

rem 2. изчакай порта да отговори, вместо да гадаем със sleep
rem    ВНИМАНИЕ: винаги 127.0.0.1, никога localhost — то резолвва първо към ::1,
rem    където никой не слуша, и всяка заявка виси ~2 секунди преди fallback.
powershell -NoProfile -Command "$n=0; while($n -lt 40){ try{ Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8765/api/state -TimeoutSec 1 | Out-Null; exit 0 }catch{ Start-Sleep -Milliseconds 250; $n++ } }; exit 1"
if errorlevel 1 (
  echo Сървърът не тръгна. Пусни ръчно: python server\app.py
  pause
  exit /b 1
)

rem 3. Chrome като приложение
set CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe
if not exist "%CHROME%" set CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe
if not exist "%CHROME%" set CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe

if exist "%CHROME%" (
  rem --autoplay-policy: Chrome otherwise refuses to start audio unless a click is still
  rem "fresh", and synthesising a report takes several seconds — so the voice was silently
  rem dropped. The page also unlocks audio on first touch; this makes it moot for this window.
  start "" "%CHROME%" --app=http://127.0.0.1:8765 --window-size=1360,900 --autoplay-policy=no-user-gesture-required
) else (
  start "" http://127.0.0.1:8765
)
exit /b 0
