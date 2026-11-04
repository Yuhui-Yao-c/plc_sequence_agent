param(
    [string]$DumpPath = 'project_files_dump_b64.txt',
    [string]$TargetDir = ''
)
$ErrorActionPreference = 'Stop'
if (!(Test-Path -LiteralPath $DumpPath)) { throw "dump file not found: $DumpPath" }
if ($TargetDir -eq '') { $TargetDir = (Get-Location).Path }
if (!(Test-Path -LiteralPath $TargetDir)) { New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null }
$TargetDir = (Resolve-Path -LiteralPath $TargetDir).Path

# Read as explicit UTF-8: file section headers contain non-ASCII relative paths.
$raw = [System.IO.File]::ReadAllText((Resolve-Path -LiteralPath $DumpPath).Path, [System.Text.Encoding]::UTF8)
$rx = [regex]'# ===== FILE: (.+?) \| (\d+) \| ([0-9A-F]{64}) =====\r?\n([0-9A-Za-z+/=\r\n]+?)\r?\n\r?\n(?=# =====)'
$sha = [System.Security.Cryptography.SHA256]::Create()
$ok = 0; $fail = 0; $totalBytes = 0
foreach ($m in $rx.Matches($raw)) {
    $rel = $m.Groups[1].Value -replace '/', '\'
    if ($rel -match '(^|\\)\.\.($|\\)' -or $rel -match '^[A-Za-z]:') {
        Write-Output ("SKIP unsafe path: " + $rel); $fail++; continue
    }
    $expected = $m.Groups[3].Value
    $size = [int]$m.Groups[2].Value
    $b64 = ($m.Groups[4].Value -split "\r?\n") -join ''
    try {
        $bytes = [Convert]::FromBase64String($b64)
        $hash = ([BitConverter]::ToString($sha.ComputeHash($bytes))) -replace '-', ''
        if ($hash -ne $expected) { throw "SHA256 mismatch" }
        if ($bytes.Length -ne $size) { throw "size mismatch" }
        $dest = Join-Path $TargetDir $rel
        $d = Split-Path $dest -Parent
        if (!(Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
        [System.IO.File]::WriteAllBytes($dest, $bytes)
        $ok++; $totalBytes += $bytes.Length
    }
    catch {
        Write-Output ("FAIL: {0}: {1}" -f $rel, $_.Exception.Message); $fail++
    }
}
$sha.Dispose()
Write-Output ("RESTORED={0} FAILED={1} TOTAL_BYTES={2} TARGET={3}" -f $ok, $fail, $totalBytes, $TargetDir)
if ($fail -gt 0) { exit 1 }
