# 安装 OpenSSH 服务端
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0

# 启动并设为开机自启
Start-Service sshd
Set-Service -Name sshd -StartupType 'Automatic'

# 放行防火墙（默认会自动加，保险起见执行）
New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH-Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22