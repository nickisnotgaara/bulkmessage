# ════════════════════════════════════════════════════════════════════════════
# Ежедневный автозапуск стенда + рассылки (локально, Windows)
# ════════════════════════════════════════════════════════════════════════════
# Запуск в 09:30 МСК, рассылка стартует в 10:00 и живёт до 20:00.
#
# ПОСЛЕДОВАТЕЛЬНОСТЬ (менять порядок нельзя):
#   1. PYTHONIOENCODING=utf-8  — консоль Windows = cp1251, без этого процесс
#      умирает МОЛЧА на первом print с эмодзи (выглядит как «сервер не поднялся»).
#   2. start_webhook.py         — сам поднимает сервер, quick-туннель И
#      переподписывает Wazzup на свежий URL.
#   3. Ждём /health через туннель, а не localhost: быстрый туннель может не
#      подняться, и тогда события уйдут в никуда.
#   4. preflight                — пункт 9 сверяет код вебхука с локальным HEAD.
#      Если он красный, боевой прогон НЕ запускаем: sender уведёт ПРИНЯТЫЕ
#      сообщения в откат, то есть в личные аккаунты.
#   5. sender --run-hours N     — считаем N от ТЕКУЩЕГО времени до 20:00 МСК.
#
# Запуск фоновой задачей агента НЕЛЬЗЯ: такие задачи чистятся вместе с сессией
# и уносят с собой вебхук (наблюдалось дважды за один день). Только Start-Process.
# ════════════════════════════════════════════════════════════════════════════

param(
    # Час старта рассылки в МСК (сейчас = +1 час до открытия активного окна).
    [int]$StartHourMsk = 10,
    # Час остановки в МСК. Окно 10:00-20:00 = 10 часов.
    [int]$StopHourMsk = 20,
    # Тестовый режим: ограничить объём и НЕ запускать при красном preflight.
    [switch]$DryMode,
    # Пробный прогон: 20 сообщений WABA, 1 час.
    [int]$WabaLimit = 0
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

# ── 1. Кодировка ────────────────────────────────────────────────────────────
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$logDir = Join-Path $ProjectRoot 'data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
$log = Join-Path $logDir "daily-$stamp.log"

function Say($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $msg
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding UTF8
}

Say "════════════════════════════════════════════════"
Say "Ежедневный запуск. Окно рассылки: ${StartHourMsk}:00 — ${StopHourMsk}:00 МСК"
Say "Лог: $log"

# ── 2. Проверка окружения до старта ─────────────────────────────────────────
if ($DryMode) {
    Say "РЕЖИМ СУХОГО ПРОГОНА: BULK_DRY_RUN=1, реальных отправок не будет."
}
else {
    $dry = (Select-String -Path '.\.env.local' -Pattern '^BULK_DRY_RUN=(\d)' |
            Select-Object -First 1).Matches.Groups[1].Value
    if ($dry -ne '0') {
        Say "⛔ БЛОКИРОВКА: в .env.local BULK_DRY_RUN=$dry, а не 0."
        Say "   Боевой прогон не запускается. Поставь BULK_DRY_RUN=0 сам."
        exit 3
    }
    Say "BULK_DRY_RUN=0 — боевой режим подтверждён владельцем."
}

# ── 3. Вебхук + туннель + переподписка Wazzup ───────────────────────────────
Say "Поднимаю вебхук, туннель и переподписываю Wazzup (start_webhook.py)..."
$webhookLog = Join-Path $logDir "webhook-$stamp.log"
$webhook = Start-Process -FilePath 'py' `
    -ArgumentList 'start_webhook.py' `
    -WorkingDirectory $ProjectRoot `
    -RedirectStandardOutput $webhookLog `
    -RedirectStandardError "$webhookLog.err" `
    -PassThru -WindowStyle Hidden
Say "Вебхук: PID $($webhook.Id), лог $webhookLog"

# ── 4. Ждём живой /health через ТУННЕЛЬ ────────────────────────────────────
$tunnelUrl = $null
$deadline = (Get-Date).AddSeconds(120)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 5
    if (Test-Path $webhookLog) {
        $m = Select-String -Path $webhookLog -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' |
              Select-Object -Last 1
        if ($m) { $tunnelUrl = $m.Matches[0].Value; break }
    }
}

if (-not $tunnelUrl) {
    Say "⛔ ТУННЕЛЬ НЕ ПОДНЯЛСЯ за 120 с. Рассылка НЕ запускается:"
    Say "   без вебхука подтверждения доставки не придут, и принятые сообщения"
    Say "   уйдут в откат — то есть в ЛИЧНЫЕ аккаунты. Лучше не отправить."
    Say "   Лог: $webhookLog"
    exit 3
}
Say "Туннель: $tunnelUrl"

# Ждём, пока /health ответит именно ЧЕРЕЗ туннель.
$health = "$tunnelUrl/health"
$healthy = $false
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri $health -TimeoutSec 10 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $healthy = $true; break }
    } catch { Start-Sleep -Seconds 5 }
}

if (-not $healthy) {
    Say "⛔ /health не ответил через туннель ($health). Рассылка НЕ запускается."
    exit 3
}
Say "✅ /health отвечает через туннель."

# ── 5. Preflight: пункт 9 (версия кода вебхука) ─────────────────────────────
Say "Проверяю preflight..."
$preOut = & py main.py preflight 2>&1 | Out-String
$preOut -split "`n" | ForEach-Object { Add-Content -Path $log -Value $_ -Encoding UTF8 }

$webhookOk = $preOut -match 'WEBHOOK CODE VERSION' -and
             ($preOut -match 'локальная версия кода' -and $preOut -notmatch 'недоступен')
if (-not $webhookOk) {
    Say "⛔ Пункт 9 preflight (версия кода вебхука) НЕ ЗЕЛЁНЫЙ."
    Say "   Вебхук держит старый код в памяти: правки репозитория не работают в бою."
    Say "   Рассылка НЕ запускается. Перезапусти вебхук и повтори."
    Say "   Подробности — в $log"
    exit 3
}
Say "✅ Пункт 9 зелёный: код вебхука совпадает с локальным."

# ── 6. Ждём открытия активного окна ────────────────────────────────────────
$now = Get-Date
$target = $now.Date.AddHours($StartHourMsk).AddMinutes(0)
if ($now -ge $target) {
    Say "Окно уже открыто (или сейчас $StartHourMsk:00) — стартую сразу."
}
else {
    $wait = [int]($target - $now).TotalSeconds
    Say "Жду $wait с до $StartHourMsk:00..."
    Start-Sleep -Seconds $wait
}

# ── 7. Считаем оставшиеся часы до $StopHourMsk:00 ──────────────────────────
$hoursLeft = [math]::Round(
    ($now.Date.AddHours($StopHourMsk) - (Get-Date)).TotalHours, 2)
if ($hoursLeft -le 0.25) {
    Say "⛔ До ${StopHourMsk}:00 осталось $hoursLeft ч — запускать поздно, СЛОМАНО."
    exit 3
}
Say "Длинный прогон на $hoursLeft ч (до ${StopHourMsk}:00 МСК)."

# ── 8. Запуск рассылки ──────────────────────────────────────────────────────
$args = @('main.py', 'sender', '--run-hours', "$hoursLeft")
if ($WabaLimit -gt 0) {
    $args += @('--waba-limit', "$WabaLimit")
    Say "Ограничение прогона: $WabaLimit сообщений WABA."
}
Say "Запускаю: py $($args -join ' ')"
$senderLog = Join-Path $logDir "sender-$stamp.log"
Start-Process -FilePath 'py' `
    -ArgumentList $args `
    -WorkingDirectory $ProjectRoot `
    -RedirectStandardOutput $senderLog `
    -RedirectStandardError "$senderLog.err" `
    -WindowStyle Hidden | Out-Null

Say "Рассылка запущена. Логи:"
Say "  вебхук : $webhookLog"
Say "  sender : $senderLog"
Say "Не давай ноутбуку уснуть до ${StopHourMsk}:00 МСК."