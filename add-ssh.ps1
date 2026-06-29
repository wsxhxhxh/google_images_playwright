# 安装 OpenSSH 服务端
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0

# 启动并设为开机自启
Start-Service sshd
Set-Service -Name sshd -StartupType 'Automatic'

# 放行防火墙（默认会自动加，保险起见执行）
New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH-Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22

# 切换到桌面目录
Set-Location "$env:USERPROFILE\Desktop"

# 克隆仓库并切换分支
git clone https://github.com/wsxhxhxh/google_images_playwright.git
Set-Location google_images_playwright
git checkout redis_desc

# 创建并写入 .env 文件（自行替换下面的内容）
$envContent = @"
PROXY_URL=https://seosystem.top/prod/api/v1/proxy-group/1/ips
HEADLESS=false
TASK_NUM=10
USE_PROXY=False
"@

# 写入文件，不存在会自动新建
$envContent | Out-File -FilePath .\.env -Encoding utf8

pip install -r requirements.txt