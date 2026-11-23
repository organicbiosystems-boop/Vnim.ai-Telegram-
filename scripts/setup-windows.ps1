# Установка бота VNIM.AI на Windows Server.
# Запуск (PowerShell от администратора):
#   irm https://raw.githubusercontent.com/organicbiosystems-boop/Vnim.ai-Telegram-/claude/festive-feynman-l0q9ji/scripts/setup-windows.ps1 | iex
# Повторный запуск обновляет код, сохраняя настройки (.env) и базу (data\).

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'

$Dir    = 'C:\vnim-bot'
$Branch = 'claude/festive-feynman-l0q9ji'
$ZipUrl = "https://github.com/organicbiosystems-boop/Vnim.ai-Telegram-/archive/refs/heads/$Branch.zip"
$Py     = 'C:\Program Files\Python312\python.exe'
$Task   = 'VnimBot'

function Step($t) { Write-Host "`n=== $t" -ForegroundColor Cyan }

# 1. Python
Step 'Python'
if (-not (Test-Path $Py)) {
    $inst = Join-Path $env:TEMP 'python-installer.exe'
    Invoke-WebRequest 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe' -OutFile $inst
    Start-Process $inst -ArgumentList '/quiet InstallAllUsers=1 PrependPath=1 Include_test=0' -Wait
    if (-not (Test-Path $Py)) { throw 'Python не установился' }
}
& $Py --version

# 2. Код бота (настройки и база сохраняются)
Step 'Скачиваю код бота'
if (Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*bot.main*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
$zip = Join-Path $env:TEMP 'vnim-bot.zip'
$tmp = Join-Path $env:TEMP 'vnim-bot-src'
Invoke-WebRequest $ZipUrl -OutFile $zip
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
Expand-Archive $zip -DestinationPath $tmp -Force
$src = Get-ChildItem $tmp -Directory | Select-Object -First 1
New-Item -ItemType Directory -Force -Path $Dir, "$Dir\data" | Out-Null
Copy-Item "$($src.FullName)\*" $Dir -Recurse -Force

# 3. Зависимости
Step 'Устанавливаю библиотеки'
if (-not (Test-Path "$Dir\venv\Scripts\python.exe")) { & $Py -m venv "$Dir\venv" }
& "$Dir\venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$Dir\venv\Scripts\python.exe" -m pip install --quiet -r "$Dir\requirements.txt"

# 4. Настройки
Step 'Настройки'
$envFile = "$Dir\.env"
if (-not (Test-Path $envFile)) {
    $token  = Read-Host 'Токен Telegram-бота (от @BotFather)'
    $apiKey = Read-Host 'Ключ Claude API (sk-ant-...)'
    $admins = Read-Host 'ID админов через запятую (ваш и партнёра)'
    $chan   = Read-Host 'Канал [Enter = @VnimAi]'
    if (-not $chan) { $chan = '@VnimAi' }
    $content = "TELEGRAM_BOT_TOKEN=$($token.Trim())`nANTHROPIC_API_KEY=$($apiKey.Trim())`nADMIN_IDS=$($admins.Trim())`nCHANNEL_ID=$($chan.Trim())`n"
    [IO.File]::WriteAllText($envFile, $content, (New-Object Text.UTF8Encoding($false)))
    Write-Host "Сохранено в $envFile"
} else {
    Write-Host "Использую существующий $envFile (чтобы поменять ключи — отредактируйте его в Блокноте)"
}

# 5. Проверка связи с Telegram
Step 'Проверяю токен Telegram'
$check = @'
import os, json, urllib.request, urllib.error
from dotenv import load_dotenv
load_dotenv()
url = "https://api.telegram.org/bot%s/getMe" % os.environ["TELEGRAM_BOT_TOKEN"]
try:
    print(urllib.request.urlopen(url, timeout=40).read().decode())
except urllib.error.HTTPError as e:
    print("ОШИБКА", e.code, e.read().decode())
except Exception as e:
    print("ОШИБКА", e)
'@
Push-Location $Dir
$check | & "$Dir\venv\Scripts\python.exe" -
Pop-Location

# 6. Автозапуск: при старте сервера и перезапуск при падении
Step 'Настраиваю автозапуск'
$cmd = "/c set PYTHONUTF8=1&& `"$Dir\venv\Scripts\python.exe`" -m bot.main >> `"$Dir\data\bot.log`" 2>&1"
$action   = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $cmd -WorkingDirectory $Dir
$trigger  = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger -Settings $settings `
    -User 'SYSTEM' -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName $Task

Start-Sleep -Seconds 20
Step 'Последние строки лога'
if (Test-Path "$Dir\data\bot.log") { Get-Content "$Dir\data\bot.log" -Tail 15 }
Write-Host "`nГотово. Бот работает в фоне и сам запускается после перезагрузки сервера." -ForegroundColor Green
Write-Host "Лог: $Dir\data\bot.log"
