<#
  Port health check / repair.

  Two different things look identical from the outside:
    1. Nobody is using the port          -> normal, service can start
    2. A process bound the port but never listens -> a connect probe reports
       "free", yet bind fails with WinError 10013 / 10048 and the service dies
       the moment its window opens.

  2026-10-23 was case 2: Sangfor's security agents (SangforPWEx / SangforSP,
  auto-start, still running after the VPN client closes) grabbed
  127.0.0.1:6185 in BOUND state. Stop them from an elevated shell, or set
  ASTRBOT_WEBUI_PORT to move AstrBot.

  Usage (Windows PowerShell):
    powershell -ExecutionPolicy Bypass -File scripts\check-ports.ps1
    powershell -ExecutionPolicy Bypass -File scripts\check-ports.ps1 -Kill
    powershell -ExecutionPolicy Bypass -File scripts\check-ports.ps1 -FixRange

  -Kill      stop leftover AstrBot / python processes that own these ports
  -FixRange  move the dynamic port range to 49152+ (reboot required)
#>
param(
    [switch]$Kill,
    [switch]$FixRange
)

$ErrorActionPreference = 'Stop'
$Ports = 6185, 6199, 6099, 7870, 7897, 7860
$OurProcesses = 'astrbot', 'python', 'pythonw', 'NapCatWinBootMain'

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-Bind([int]$port) {
    $sock = $null
    try {
        $sock = [System.Net.Sockets.Socket]::new('InterNetwork', 'Stream', 'Tcp')
        $sock.Bind([System.Net.IPEndPoint]::new([System.Net.IPAddress]::Any, $port))
        return $true
    } catch {
        return $false
    } finally {
        if ($sock) { $sock.Close() }
    }
}

function Test-Listen([int]$port) {
    try {
        $c = [System.Net.Sockets.TcpClient]::new()
        $c.Connect('127.0.0.1', $port)
        $c.Close()
        return $true
    } catch {
        return $false
    }
}

function Get-PortOwners([int]$port) {
    $rows = netstat -ano | Select-String (":{0}\s" -f $port)
    $pids = foreach ($row in $rows) {
        $f = $row.Line.Trim() -split '\s+'
        if ($f.Length -ge 5 -and $f[-1] -match '^\d+$' -and $f[-1] -ne '0') { $f[-1] }
    }
    foreach ($procId in ($pids | Sort-Object -Unique)) {
        $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($p) {
            $exe = ''
            try { $exe = $p.Path } catch { $exe = '(not visible, run as admin)' }
            [pscustomobject]@{ Pid = $procId; Name = $p.ProcessName; Path = $exe }
        } else {
            [pscustomobject]@{ Pid = $procId; Name = '(gone)'; Path = '' }
        }
    }
}

Write-Host ''
Write-Host '=== port state ===' -ForegroundColor Cyan
$toKill = @()
foreach ($port in $Ports) {
    $listen = Test-Listen $port
    $bindable = Test-Bind $port
    if ($listen) {
        $mark = 'running'; $color = 'Green'
    } elseif (-not $bindable) {
        $mark = 'LOCKED'; $color = 'Red'
    } else {
        $mark = 'free'; $color = 'Gray'
    }
    Write-Host ("{0,-6} {1,8}   listen={2,-5} bindable={3}" -f $port, $mark, $listen, $bindable) -ForegroundColor $color

    if (-not $listen -or -not $bindable) {
        foreach ($owner in Get-PortOwners $port) {
            Write-Host ("         owner: pid={0} {1} {2}" -f $owner.Pid, $owner.Name, $owner.Path)
            if ($Kill -and $OurProcesses -contains $owner.Name) {
                $toKill += $owner.Pid
            }
        }
    }
}

Write-Host ''
Write-Host '=== dynamic port range ===' -ForegroundColor Cyan
$range = netsh int ipv4 show dynamicport tcp
$range | Write-Host
$startLine = $range | Select-String 'Start Port'
$countLine = $range | Select-String 'Number of Ports'
if ($startLine -and $countLine) {
    $start = [int](($startLine.Line -split ':')[-1].Trim())
    $count = [int](($countLine.Line -split ':')[-1].Trim())
    $end = $start + $count - 1
    $overlap = $Ports | Where-Object { $_ -ge $start -and $_ -le $end }
    if ($overlap) {
        Write-Host ("  WARNING: {0} sit inside {1}-{2}, so Windows may hand them out as" -f ($overlap -join ','), $start, $end) -ForegroundColor Yellow
        Write-Host '  ephemeral source ports. That is how a port ends up "held by something' -ForegroundColor Yellow
        Write-Host '  that is not listening". Use -FixRange to fix it.' -ForegroundColor Yellow
    } else {
        Write-Host '  None of our ports are inside the dynamic range. Good.' -ForegroundColor Green
    }
}

if ($toKill.Count -gt 0) {
    Write-Host ''
    Write-Host '=== stopping leftovers ===' -ForegroundColor Cyan
    foreach ($procId in ($toKill | Sort-Object -Unique)) {
        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Host ("  killed pid {0}" -f $procId) -ForegroundColor Green
        } catch {
            Write-Host ("  pid {0}: {1}" -f $procId, $_.Exception.Message) -ForegroundColor Red
        }
    }
}

if ($FixRange) {
    Write-Host ''
    Write-Host '=== dynamic port range -> start 49152 ===' -ForegroundColor Cyan
    if (-not (Test-Admin)) {
        Write-Host '  needs an elevated shell, skipped.' -ForegroundColor Red
    } else {
        netsh int ipv4 set dynamicport tcp start=49152 num=16384 | Out-Null
        netsh int ipv4 set dynamicport udp start=49152 num=16384 | Out-Null
        netsh int ipv6 set dynamicport tcp start=49152 num=16384 | Out-Null
        Write-Host '  done, takes effect after reboot.' -ForegroundColor Green
    }
}

Write-Host ''
Write-Host '=== result ===' -ForegroundColor Cyan
$bad = $Ports | Where-Object { -not (Test-Listen $_) -and -not (Test-Bind $_) }
if ($bad) {
    Write-Host ("  not bindable: {0}" -f ($bad -join ',')) -ForegroundColor Red
    Write-Host '  The owner may be a protected service: stop it elevated, or set ASTRBOT_WEBUI_PORT.'
    exit 1
}
Write-Host '  all ports healthy.' -ForegroundColor Green
exit 0
