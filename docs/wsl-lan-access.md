# WSL Web 局域网访问

当前配置：

- Windows WLAN IP：`172.16.186.179`
- Web 端口：`18989`
- 访问地址：<http://172.16.186.179:18989/>
- WSL IP 会在重启后变化，因此转发失效时需要重建。

## 1. 启动 Web 服务

在 WSL 中执行：

```bash
cd ~/docAgent
conda activate py31115
python -m weekly_report_agent.web --host 0.0.0.0 --port 18989
```

保持该终端运行。本机检查：

```bash
curl -I http://127.0.0.1:18989/
```

## 2. 首次配置或 WSL 重启后重建

以**管理员身份**打开 Windows PowerShell，整段执行：

```powershell
$Port = 18989
$WindowsIp = "172.16.186.179"  # 当前 WLAN IPv4；切换网络后需修改
$LanSubnet = "172.16.186.0/24" # 当前 WLAN 网段；切换网络后需修改
$RuleName = "Weekly Report Web 18989"
$WslIp = ((wsl.exe hostname -I).Trim() -split '\s+')[0]

# 删除当前入口的旧转发，再指向最新 WSL IP
netsh interface portproxy delete v4tov4 `
  listenaddress=$WindowsIp listenport=$Port 2>$null | Out-Null
netsh interface portproxy add v4tov4 `
  listenaddress=$WindowsIp listenport=$Port `
  connectaddress=$WslIp connectport=$Port

# 刷新本项目防火墙规则
Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue |
  Remove-NetFirewallRule
New-NetFirewallRule `
  -DisplayName $RuleName -Direction Inbound -Action Allow `
  -Protocol TCP -LocalAddress $WindowsIp -LocalPort $Port `
  -RemoteAddress $LanSubnet -Profile Any | Out-Null

Write-Host "WSL IP: $WslIp"
Write-Host "访问地址: http://${WindowsIp}:${Port}/"
```

如果 Windows WLAN IP 变化，可用 `ipconfig` 查询带默认网关的 `WLAN` IPv4，并修改 `$WindowsIp` 和 `$LanSubnet`。

## 3. 验证

在 Windows PowerShell 中执行：

```powershell
Test-NetConnection 172.16.186.179 -Port 18989
netsh interface portproxy show v4tov4
```

`TcpTestSucceeded` 应为 `True`。同一局域网中的机器访问：

```text
http://172.16.186.179:18989/
```

如果 Windows 本机能访问而其他机器不能访问，通常是公司 WLAN 终端隔离、VPN 或企业防火墙策略导致。

## 4. 清理转发规则

查看全部规则：

```powershell
netsh interface portproxy show v4tov4
```

删除本项目当前规则（管理员 PowerShell）：

```powershell
netsh interface portproxy delete v4tov4 `
  listenaddress=172.16.186.179 listenport=18989
Remove-NetFirewallRule `
  -DisplayName "Weekly Report Web 18989" -ErrorAction SilentlyContinue
```

如果列表中还有旧 IP 的 `18989` 规则，将 `listenaddress` 替换成列表中的旧地址后再次删除。不要使用 `netsh interface portproxy reset`，它会删除其他项目的全部转发规则。
