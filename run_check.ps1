Set-Location $PSScriptRoot

$logPath = Join-Path $PSScriptRoot "run_check.log"
if (Test-Path $logPath) {
    $age = (Get-Date) - (Get-Item $logPath).CreationTime
    if ($age.TotalHours -ge 24) {
        Remove-Item $logPath -Force
    }
}

try { git pull --rebase --autostash origin master *>> run_check.log } catch {}

& ".\.venv\Scripts\python.exe" "watcher.py" "--once" *>> run_check.log

git add state.json *>> run_check.log
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m "update state (local)" *>> run_check.log
    git push origin master *>> run_check.log
    if ($LASTEXITCODE -ne 0) {
        try { git pull --rebase origin master *>> run_check.log } catch {}
        git push origin master *>> run_check.log
    }
}
