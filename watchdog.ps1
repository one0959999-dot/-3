# 백테스트 워치독 — 5분마다 작업스케줄러가 호출.
# run_backtest 가 안 돌면 래퍼를 살리고, 절전방지(keep_awake)도 보장한다.
# 래퍼/키프어웨이에 단일 가드가 있어 중복 실행은 자동 방지됨.
$dir = 'C:\Users\신동호\.gemini\antigravity\scratch\lassi_bot'
Set-Location $dir
$log = Join-Path $dir 'watchdog.log'

# 1) run_backtest.py python 살아있나
$py = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -like 'python*' -and $_.CommandLine -like '*run_backtest.py*'
}
# 2) 래퍼 살아있나
$wrap = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object {
    $_.CommandLine -like '*-File*run_backtest_forever*'
}
if (-not $py -and -not $wrap) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [watchdog] 백테스트 죽음 → 래퍼 재기동" | Out-File -FilePath $log -Append -Encoding utf8
    Start-Process powershell -ArgumentList '-ExecutionPolicy','Bypass','-File','run_backtest_forever.ps1' -WorkingDirectory $dir -WindowStyle Hidden
}

# 3) 절전방지 살아있나
$ka = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object {
    $_.CommandLine -like '*keep_awake*'
}
if (-not $ka) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [watchdog] 절전방지 죽음 → 재기동" | Out-File -FilePath $log -Append -Encoding utf8
    Start-Process powershell -ArgumentList '-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',(Join-Path $dir 'keep_awake.ps1') -WindowStyle Hidden
}

# 4) 진행률 리포터 살아있나
$rep = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -like 'python*' -and $_.CommandLine -like '*progress_reporter*'
}
if (-not $rep) {
    $py = "C:\Users\신동호\AppData\Local\Programs\Python\Python312\python.exe"
    if (-not (Test-Path $py)) { $py = 'python' }
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [watchdog] 리포터 죽음 → 재기동" | Out-File -FilePath $log -Append -Encoding utf8
    Start-Process $py -ArgumentList '-B','progress_reporter.py' -WorkingDirectory $dir -WindowStyle Hidden
}
