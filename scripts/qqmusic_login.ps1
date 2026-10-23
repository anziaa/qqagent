# QQ 音乐扫码登录 —— 双击「QQ音乐扫码登录.bat」运行，不要直接右键运行本文件
# 流程：申请二维码 -> 自动弹出图片 -> 手机扫码 -> 凭证写入 AstrBot
# 位置：<仓库>\scripts\  （登录脚本在同目录 tools\ 下，二维码和状态日志也落在那里，都不入库）
#
# 用哪个 python：优先环境变量 QQAGENT_PYTHON，其次仓库内的 venv，最后 PATH 里的 python。
# 换过环境不用改这个文件，设个环境变量就行。

$ErrorActionPreference = 'Stop'

$root   = Split-Path $PSScriptRoot -Parent
$script = Join-Path $PSScriptRoot 'tools\qqmusic_login.py'
$qr     = Join-Path $PSScriptRoot 'tools\qq_login_qr.png'
$log    = Join-Path $PSScriptRoot 'tools\login_status.log'

function Resolve-Python {
    if ($env:QQAGENT_PYTHON -and (Test-Path $env:QQAGENT_PYTHON)) { return $env:QQAGENT_PYTHON }
    foreach ($c in @(
        (Join-Path $root 'runtime\astrbot\.venv\Scripts\python.exe'),
        (Join-Path $root '.venv\Scripts\python.exe'),
        (Join-Path $root 'venv\Scripts\python.exe')
    )) {
        if (Test-Path $c) { return $c }
    }
    $found = Get-Command python -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    return ''
}

$py = Resolve-Python

if (-not $py -or -not (Test-Path $py)) {
    Write-Host "[X] 找不到可用的 python" -ForegroundColor Red
    Write-Host "    设环境变量 QQAGENT_PYTHON 指向 AstrBot 那个环境的 python.exe，或者把它放进 PATH。" -ForegroundColor DarkGray
    exit 1
}
if (-not (Test-Path $script)) {
    Write-Host "[X] 找不到登录脚本：$script" -ForegroundColor Red
    exit 1
}

# 清掉残留的 PYTHONPATH，否则可能加载到错误的包
$env:PYTHONPATH = ''
$env:PYTHONIOENCODING = 'utf-8'

# 删掉上一次的二维码和日志，避免把过期图片当成新的
if (Test-Path $qr)  { Remove-Item $qr  -Force -ErrorAction SilentlyContinue }
if (Test-Path $log) { Remove-Item $log -Force -ErrorAction SilentlyContinue }

$t0 = Get-Date
Write-Host "正在向 QQ 音乐申请二维码..." -ForegroundColor Cyan
$p = Start-Process -FilePath $py -ArgumentList @('-u', $script) -NoNewWindow -PassThru

# 只认「本次新生成」的 png：既存在、又比开始时间新，避免拿到残留的过期二维码
function Test-FreshQr {
    if (-not (Test-Path $qr)) { return $false }
    return ((Get-Item $qr -ErrorAction SilentlyContinue).LastWriteTime -gt $t0)
}

# 等 png 落盘（正常 1-2 秒），最多等 30 秒
$deadline = (Get-Date).AddSeconds(30)
while (-not (Test-FreshQr) -and (Get-Date) -lt $deadline) {
    if ($p.HasExited) { break }
    Start-Sleep -Milliseconds 300
}

if (Test-FreshQr) {
    Write-Host ""
    Write-Host "二维码已生成，正在打开图片 —— 有效期约 3 分钟。" -ForegroundColor Green
    Write-Host "手机打开 QQ音乐 -> 我的 -> 右上角扫一扫 -> 确认登录" -ForegroundColor Green
    Write-Host ""
    Start-Sleep -Milliseconds 300
    Start-Process $qr
} else {
    Write-Host ""
    Write-Host "[!] 没等到二维码文件，看下面 python 的输出。" -ForegroundColor Yellow
}

$p.WaitForExit()
Write-Host ""

# 退出码在这个环境里取不到，直接读脚本自己写的状态日志判断结果
$state = ''
if (Test-Path $log) {
    $lines = @(Get-Content $log -ErrorAction SilentlyContinue)
    if ($lines.Count -gt 0) { $state = $lines[-1] }
}

if ($state -match 'LOGIN_OK') {
    Write-Host "[OK] 登录成功，凭证已写入 qqmusic_cred.json" -ForegroundColor Green
    Write-Host "     记得重启 AstrBot（或重载 astrbot_plugin_music），否则它还在用内存里的旧凭证。" -ForegroundColor Green
} elseif ($state -match 'FAILED|ERROR') {
    Write-Host "[X] 登录没有完成：$state" -ForegroundColor Red
    Write-Host "    超时或被拒绝的话，原样重跑一次即可；其他情况手动跑一遍看完整报错：" -ForegroundColor DarkGray
    Write-Host "    `"$py`" -u `"$script`"" -ForegroundColor DarkGray
} else {
    Write-Host "[X] 登录进程提前退出了，没有拿到结果。" -ForegroundColor Red
    Write-Host "    完整原因看上面的 python 输出；也可以手动跑一遍：" -ForegroundColor DarkGray
    Write-Host "    `"$py`" -u `"$script`"" -ForegroundColor DarkGray
}
