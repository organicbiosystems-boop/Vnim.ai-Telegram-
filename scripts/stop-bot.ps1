# Остановить бота на сервере (не удаляя его). Запустить снова: scripts/update-keys.ps1
Disable-ScheduledTask -TaskName 'VnimBot' -ErrorAction SilentlyContinue | Out-Null
Stop-ScheduledTask -TaskName 'VnimBot' -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*bot.main*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host 'Бот остановлен.' -ForegroundColor Yellow
