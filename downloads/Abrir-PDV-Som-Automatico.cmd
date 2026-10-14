@echo off
setlocal
set "sahara_chrome="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "sahara_chrome=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined sahara_chrome if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "sahara_chrome=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined sahara_chrome if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "sahara_chrome=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not defined sahara_chrome (
  echo Google Chrome nao encontrado. Instale o Chrome e tente novamente.
  pause
  exit /b 1
)
echo Abrindo o PDV com audio automatico em uma janela exclusiva.
start "" "%sahara_chrome%" --autoplay-policy=no-user-gesture-required "--user-data-dir=%LOCALAPPDATA%\SaharaPDV-Som-Automatico" "--app=https://sahara-esfihas-pdv.onrender.com/admin.html"
endlocal
