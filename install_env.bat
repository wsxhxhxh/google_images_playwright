@echo off
chcp 65001 >nul
color 0A
echo ====================================================
echo          全自动部署：软件+Redis+Firefox+项目
echo          无需手动点击，全程自动完成
echo ====================================================
echo.

:: 目录配置
set "DOWN_DIR=C:\Users\Administrator\Downloads"
set "DESKTOP_DIR=C:\Users\Administrator\Desktop"
if not exist "%DOWN_DIR%" mkdir "%DOWN_DIR%"

echo ========== 1. 下载所有工具 ==========
echo.

echo [1/7] 下载 Python...
powershell -command "Invoke-WebRequest -Uri 'https://github.com/wsxhxhxh/google_images_playwright/releases/download/1.0.0/python-3.10.5-amd64.exe' -OutFile '%DOWN_DIR%\python-3.10.5-amd64.exe' -UseBasicParsing"

echo [2/7] 下载 Notepad++...
powershell -command "Invoke-WebRequest -Uri 'https://github.com/wsxhxhxh/google_images_playwright/releases/download/1.0.0/npp.8.9.1.Installer.x64.exe' -OutFile '%DOWN_DIR%\npp.8.9.1.Installer.x64.exe' -UseBasicParsing"

echo [3/7] 下载 VC++...
powershell -command "Invoke-WebRequest -Uri 'https://github.com/wsxhxhxh/google_images_playwright/releases/download/1.0.0/VC_redist.x64.exe' -OutFile '%DOWN_DIR%\VC_redist.x64.exe' -UseBasicParsing"

echo [4/7] 下载 Git...
powershell -command "Invoke-WebRequest -Uri 'https://github.com/wsxhxhxh/google_images_playwright/releases/download/1.0.0/Git-2.53.0-64-bit.exe' -OutFile '%DOWN_DIR%\Git-2.53.0-64-bit.exe' -UseBasicParsing"

echo [5/7] 下载 Chrome...
powershell -command "Invoke-WebRequest -Uri 'https://github.com/wsxhxhxh/google_images_playwright/releases/download/1.0.0/ChromeSetup.exe' -OutFile '%DOWN_DIR%\ChromeSetup.exe' -UseBasicParsing"

echo [6/7] 下载 Redis...
powershell -command "Invoke-WebRequest -Uri 'https://github.com/wsxhxhxh/google_images_playwright/releases/download/1.0.0/Redis-8.8.0-Windows-x64-msys2-with-Service.zip' -OutFile '%DOWN_DIR%\Redis.zip' -UseBasicParsing"

echo [7/7] 下载 Firefox...
powershell -command "Invoke-WebRequest -Uri 'https://github.com/wsxhxhxh/google_images_playwright/releases/download/1.0.0/firefox-151.0a1.en-US.win64.zip' -OutFile '%DOWN_DIR%\firefox.zip' -UseBasicParsing"

echo.
echo ========== 2. 静默安装软件 ==========
echo.

echo [1/6] 安装 Python...
"%DOWN_DIR%\python-3.10.5-amd64.exe" /passive InstallAllUsers=0 PrependPath=1

echo [2/6] 安装 Notepad++...
"%DOWN_DIR%\npp.8.9.1.Installer.x64.exe" /S

echo [3/6] 安装 VC++...
"%DOWN_DIR%\VC_redist.x64.exe" /quiet /norestart

echo [4/6] 安装 Git...
"%DOWN_DIR%\Git-2.53.0-64-bit.exe" /VERYSILENT /NORESTART

echo [5/6] 安装 Chrome...
"%DOWN_DIR%\ChromeSetup.exe" /silent /install

echo [6/6] 安装 Redis 服务...
if exist "C:\Redis" rd /s /q "C:\Redis"
powershell -Command "Expand-Archive -Path '%DOWN_DIR%\Redis.zip' -DestinationPath 'C:\' -Force"
ren "C:\Redis-8.8.0-Windows-x64-msys2" "Redis"
cd /d "C:\Redis"
start /wait RedisService.exe install

echo.
echo ========== 3. 部署 Firefox ==========
echo.

set "FF_DEST=C:\Program Files"
if exist "%FF_DEST%\Mozilla Firefox" rd /s /q "%FF_DEST%\Mozilla Firefox"
if exist "%FF_DEST%\firefox" rd /s /q "%FF_DEST%\firefox"

powershell -Command "Expand-Archive -Path '%DOWN_DIR%\firefox.zip' -DestinationPath '%FF_DEST%' -Force"
ren "%FF_DEST%\firefox" "Mozilla Firefox"

echo.
echo ========== 4. 克隆项目到桌面 ==========
echo.

cd /d "%DESKTOP_DIR%"
if exist "google_images_playwright" rd /s /q "google_images_playwright"
git clone https://github.com/wsxhxhxh/google_images_playwright.git

cd google_images_playwright
git branch --set-upstream-to=origin/main main
git pull

echo.
echo ====================================================
echo               ✅ 全部部署完成！
echo ====================================================
echo 已完成：
echo 1. Python、Notepad++、VC++、Git、Chrome
echo 2. Redis 已安装为服务：C:\Redis
echo 3. Firefox 已部署：C:\Program Files\Mozilla Firefox
echo 4. 项目已克隆到桌面，git pull 已正常可用
echo.
pause
exit