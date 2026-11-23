# Замена токена бота и/или ключа Claude на сервере и перезапуск бота.
# Запуск:
#   irm https://raw.githubusercontent.com/organicbiosystems-boop/Vnim.ai-Telegram-/claude/festive-feynman-l0q9ji/scripts/update-keys.ps1 | iex

$ErrorActionPreference = 'Stop'
$Dir  = 'C:\vnim-bot'
$File = "$Dir\.env"
$Task = 'VnimBot'

if (-not (Test-Path $File)) { throw "Не найден $File — сначала запустите установщик setup-windows.ps1" }

$token = (Read-Host 'Новый токен Telegram-бота (Enter = оставить старый)').Trim()
$key   = (Read-Host 'Новый ключ Claude API (Enter = оставить старый)').Trim()

$lines = Get-Content $File
if ($token) { $lines = $lines -replace '^TELEGRAM_BOT_TOKEN=.*', "TELEGRAM_BOT_TOKEN=$token" }
if ($key)   { $lines = $lines -replace '^ANTHROPIC_API_KEY=.*', "ANTHROPIC_API_KEY=$key" }
[IO.File]::WriteAllText($File, (($lines -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))
Write-Host 'Настройки сохранены.'

# Проверка токена
$tok = (($lines | Where-Object { $_ -like 'TELEGRAM_BOT_TOKEN=*' }) -replace '^TELEGRAM_BOT_TOKEN=', '')
try {
    $me = Invoke-RestMethod "https://api.telegram.org/bot$tok/getMe" -TimeoutSec 40
    Write-Host "Telegram: OK, бот @$($me.result.username)" -ForegroundColor Green
} catch {
    Write-Host "Telegram: ОШИБКА — $($_.Exception.Message)" -ForegroundColor Red
}

# Перезапуск бота
Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*bot.main*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-ScheduledTask -TaskName $Task
Start-Sleep -Seconds 15
Get-Content "$Dir\data\bot.log" -Tail 8 -Encoding UTF8
Write-Host "`nБот перезапущен." -ForegroundColor Green
