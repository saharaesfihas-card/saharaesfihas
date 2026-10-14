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
echo Abrindo o PDV com impressao direta na impressora padrao.
echo A comanda ja tem DUAS vias. Configure apenas UMA copia no driver.
start "" "%sahara_chrome%" --kiosk-printing "--user-data-dir=%LOCALAPPDATA%\SaharaPDV-Impressao" "--app=https://sahara-esfihas-pdv.onrender.com/admin.html"
endlocal
