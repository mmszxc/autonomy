param(
    [string]$Profile = "default",
    [int]$Count = 3,
    [switch]$Full,
    [switch]$Seeds,
    [switch]$Tools
)

# 使用者本机查看脚本：只调用本机已安装的 hermes，不包含远端地址或密钥。
[Console]::OutputEncoding = [Text.Encoding]::UTF8
chcp 65001 | Out-Null

$argsList = @()
if ($Profile -ne "default") { $argsList += @("-p", $Profile) }
$argsList += @("autonomy", "log", "-n", $Count)
if ($Full) { $argsList += "--full" }
if ($Seeds) { $argsList += "--seeds" }
if ($Tools) { $argsList += "--tools" }

& hermes @argsList
exit $LASTEXITCODE
