<#
.SYNOPSIS
    Siapkan flashbuy di laptop Windows: cek Python 3.12 / Chrome / adb, buat venv, pip install -e ., dan
    (bila HP terhubung) python -m uiautomator2 init.

.DESCRIPTION
    - Kompatibel Windows PowerShell 5.1 dan PowerShell 7. Tanpa hak admin.
    - Idempoten: aman dijalankan berulang (venv & target.yaml yang sudah ada tidak ditimpa).
    - Tidak mengunduh lalu menjalankan skrip dari luar; yang mengunduh hanya pip (paket Python) dan
      `python -m uiautomator2 init` (agent uiautomator2 ke HP).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup.ps1
    pwsh -File .\setup.ps1 -SkipDevice
#>
[CmdletBinding()]
param(
    [switch]$SkipDevice  # lewati `uiautomator2 init` walaupun HP terhubung
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$Root = $PSScriptRoot
$Venv = Join-Path $Root '.venv'
$VenvPython = Join-Path (Join-Path $Venv 'Scripts') 'python.exe'
$script:Problems = @()
$script:Warnings = @()

function Write-Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "    OK  $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) {
    Write-Host "    !   $Text" -ForegroundColor Yellow
    $script:Warnings += $Text
}
function Write-Fail([string]$Text) {
    Write-Host "    X   $Text" -ForegroundColor Red
    $script:Problems += $Text
}

function Get-PythonVersion([string]$Exe, [string[]]$PreArgs) {
    # Versi "3.12.x" dari interpreter, atau $null bila tidak bisa dijalankan.
    try {
        # tanpa tanda kutip di dalam argumen: PowerShell 5.1 tidak meloloskan " ke program native dengan benar
        $out = & $Exe @PreArgs -c 'import platform; print(platform.python_version())' 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return ([string]$out).Trim() }
    } catch { }
    return $null
}

function Find-Python312 {
    # 1) py launcher (py -3.12), 2) python di PATH (bukan alias Microsoft Store). Return objek / $null.
    $py = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($py) {
        $v = Get-PythonVersion $py.Path @('-3.12')
        if ($v -and $v.StartsWith('3.12.')) {
            return [pscustomobject]@{ Exe = $py.Path; PreArgs = @('-3.12'); Version = $v }
        }
    }
    foreach ($name in @('python', 'python3')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        if ($cmd.Path -like '*\WindowsApps\*') {
            Write-Warn "$name di PATH adalah alias Microsoft Store ($($cmd.Path)); diabaikan"
            continue
        }
        $v = Get-PythonVersion $cmd.Path @()
        if ($v -and $v.StartsWith('3.12.')) {
            return [pscustomobject]@{ Exe = $cmd.Path; PreArgs = @(); Version = $v }
        }
        if ($v) { Write-Warn "$name di PATH versi $v (butuh 3.12.x)" }
    }
    return $null
}

function Find-Chrome {
    $candidates = @()
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
        if ($base) { $candidates += (Join-Path $base 'Google\Chrome\Application\chrome.exe') }
    }
    foreach ($key in @('HKCU:\Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe',
                       'HKLM:\Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe')) {
        try {
            $item = Get-ItemProperty -Path $key -ErrorAction Stop
            if ($item.'(default)') { $candidates += $item.'(default)' }
        } catch { }
    }
    $cmd = Get-Command 'chrome' -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Path }
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { return $c }
    }
    return $null
}

function Get-AdbDevices([string]$Adb) {
    # Baris "<serial>\t<status>" dari `adb devices`.
    $lines = & $Adb devices 2>$null
    $devices = @()
    foreach ($line in $lines) {
        if ($line -match '^(\S+)\s+(device|unauthorized|offline)\s*$') {
            $devices += [pscustomobject]@{ Serial = $Matches[1]; Status = $Matches[2] }
        }
    }
    return $devices
}

Write-Host "flashbuy setup ($Root)" -ForegroundColor White
Write-Host "PowerShell $($PSVersionTable.PSVersion) | tanpa hak admin | aman dijalankan ulang"

# ---------------------------------------------------------------- 1. Python 3.12
Write-Step 'Python 3.12'
$python = Find-Python312
if ($python) {
    Write-Ok "Python $($python.Version) ($($python.Exe) $($python.PreArgs -join ' '))"
} else {
    Write-Fail ('Python 3.12 tidak ditemukan. Pasang dari https://www.python.org/downloads/ (centang "Add ' +
                'python.exe to PATH" atau gunakan py launcher), lalu jalankan setup.ps1 lagi.')
}

# ---------------------------------------------------------------- 2. Chrome
Write-Step 'Google Chrome (jalur web, channel "chrome")'
$chrome = Find-Chrome
if ($chrome) {
    Write-Ok $chrome
} else {
    Write-Warn ('Chrome tidak ditemukan. Pasang Google Chrome, atau set web.channel: null di target.yaml lalu ' +
                'pasang Chromium Playwright secara manual (python -m playwright install chromium).')
}

# ---------------------------------------------------------------- 3. adb
Write-Step 'adb (jalur Android)'
$adbCmd = Get-Command 'adb' -ErrorAction SilentlyContinue
$adb = $null
if ($adbCmd) {
    $adb = $adbCmd.Path
    Write-Ok $adb
} else {
    Write-Warn ('adb tidak ada di PATH. Pasang Android SDK Platform-Tools (developer.android.com/tools/releases/' +
                'platform-tools), tambahkan foldernya ke PATH, lalu buka terminal baru. Tanpa adb hanya jalur web.')
}

if ($script:Problems.Count -gt 0) {
    Write-Host ''
    Write-Host 'Setup berhenti: perbaiki masalah di atas lalu jalankan lagi.' -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- 4. venv
Write-Step "Virtualenv $Venv"
if (Test-Path -LiteralPath $VenvPython) {
    $v = Get-PythonVersion $VenvPython @()
    if ($v -and $v.StartsWith('3.12.')) {
        Write-Ok "sudah ada (Python $v), dipakai ulang"
    } else {
        Write-Host "    X   venv memakai Python $v (butuh 3.12). Hapus folder .venv lalu jalankan setup.ps1 lagi." `
            -ForegroundColor Red
        exit 1
    }
} else {
    $pyArgs = @($python.PreArgs) + @('-m', 'venv', $Venv)
    & $python.Exe @pyArgs
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        Write-Host '    X   gagal membuat venv' -ForegroundColor Red
        exit 1
    }
    Write-Ok 'dibuat'
}

# ---------------------------------------------------------------- 5. pip install -e .
Write-Step 'pip install -e . (paket flashbuy + dependensi)'
Push-Location $Root
try {
    & $VenvPython -m pip install --disable-pip-version-check -e .
    if ($LASTEXITCODE -ne 0) {
        Write-Host '    X   pip install gagal (cek koneksi internet / proxy)' -ForegroundColor Red
        exit 1
    }
} finally {
    Pop-Location
}
Write-Ok 'terpasang'

# ---------------------------------------------------------------- 6. target.yaml
Write-Step 'target.yaml'
$target = Join-Path $Root 'target.yaml'
if (Test-Path -LiteralPath $target) {
    Write-Ok 'sudah ada (tidak ditimpa)'
} else {
    Copy-Item -LiteralPath (Join-Path $Root 'target.example.yaml') -Destination $target
    Write-Ok 'disalin dari target.example.yaml - isi product_url, start_time, harga, expected_name'
}

# ---------------------------------------------------------------- 7. HP: uiautomator2 init
Write-Step 'HP Android (uiautomator2 init)'
if ($SkipDevice) {
    Write-Warn 'dilewati (-SkipDevice)'
} elseif (-not $adb) {
    Write-Warn 'dilewati (adb tidak ada)'
} else {
    $devices = @(Get-AdbDevices $adb)
    $ready = @($devices | Where-Object { $_.Status -eq 'device' })
    foreach ($d in @($devices | Where-Object { $_.Status -ne 'device' })) {
        Write-Warn "HP $($d.Serial) berstatus $($d.Status): buka kunci HP dan setujui dialog 'Izinkan USB debugging'"
    }
    if ($ready.Count -eq 0) {
        Write-Warn 'tidak ada HP terhubung (aktifkan USB debugging, colok kabel, jalankan setup.ps1 lagi)'
    } else {
        foreach ($d in $ready) {
            & $VenvPython -m uiautomator2 init --serial $d.Serial
            if ($LASTEXITCODE -eq 0) {
                Write-Ok "agent uiautomator2 terpasang di $($d.Serial)"
            } else {
                Write-Warn "uiautomator2 init gagal di $($d.Serial); jalankan ulang setelah HP terbuka kuncinya"
            }
        }
    }
}

# ---------------------------------------------------------------- selesai
Write-Host ''
Write-Host 'Setup selesai.' -ForegroundColor Green
if ($script:Warnings.Count -gt 0) {
    Write-Host "Peringatan: $($script:Warnings.Count) (lihat tanda ! di atas)." -ForegroundColor Yellow
}
Write-Host @'

Langkah berikutnya (H-1), dari folder ini:
  .\.venv\Scripts\Activate.ps1
  1. Login web (sekali, manual, termasuk OTP; tutup browser setelah selesai):
       python -m flashbuy login --platform web --config target.yaml
     Login aplikasi Shopee di HP (manual):
       python -m flashbuy login --platform android --config target.yaml
  2. Kalibrasi di produk biasa yang murah (Anda yang men-tap; alat tidak mengklik "Buat Pesanan"):
       python -m flashbuy calibrate --platform web --config target.yaml --url <URL produk murah>
       python -m flashbuy calibrate --platform android --config target.yaml
  3. Dry-run (berhenti sebelum "Buat Pesanan"):
       python -m flashbuy run --config target.yaml
  4. Cek akhir:
       python -m flashbuy doctor --config target.yaml --beep
Hari H: doctor lalu `python -m flashbuy run --config target.yaml --live` (paling lambat T-70 detik; idealnya
sebelum T-10 menit).
'@
exit 0
