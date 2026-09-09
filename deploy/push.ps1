# Заливка проекта на сервер с Windows. Запускать из корня проекта:
#   powershell -ExecutionPolicy Bypass -File deploy\push.ps1 -Server root@СЕРВЕР
#
# Копирует только то, что нужно в проде: код, Dockerfile, compose, requirements, .env.
# Не тащит .venv, .git, локальную базу, логи и превью картинок.
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [string]$AppDir = "/opt/cu-workout-tracker",
    [switch]$SkipEnv        # не перезаписывать .env, который уже лежит на сервере
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$dirs  = @("app", "tests", "deploy")
$files = @("Dockerfile", "docker-compose.yml", "requirements.txt", "seed_trainings.py",
           ".env.example", "README.md")
if (-not $SkipEnv) { $files += ".env" }

foreach ($item in ($dirs + $files)) {
    if (-not (Test-Path (Join-Path $root $item))) {
        throw "В проекте нет '$item' — запускай скрипт из корня проекта."
    }
}

Write-Host "==> Создаю $AppDir на сервере" -ForegroundColor Cyan
ssh $Server "mkdir -p $AppDir"
if ($LASTEXITCODE -ne 0) { throw "Не удалось подключиться к $Server" }

# Каталоги пакуем в архив: так не тащим __pycache__ и не делаем scp на каждый файл.
# Через временный файл, а не через конвейер: PowerShell портит бинарный поток.
$tar = Join-Path $env:TEMP "cu-workout-deploy.tar"
Write-Host "==> Пакую $($dirs -join ', ')" -ForegroundColor Cyan
tar -cf $tar -C $root --exclude="__pycache__" --exclude="*.pyc" @dirs
if ($LASTEXITCODE -ne 0) { throw "tar не смог собрать архив" }

try {
    Write-Host "==> Копирую архив" -ForegroundColor Cyan
    scp -q $tar "${Server}:/tmp/cu-workout-deploy.tar"
    if ($LASTEXITCODE -ne 0) { throw "scp не смог скопировать архив" }

    ssh $Server "rm -rf $AppDir/app $AppDir/tests $AppDir/deploy && tar -xf /tmp/cu-workout-deploy.tar -C $AppDir && rm -f /tmp/cu-workout-deploy.tar"
    if ($LASTEXITCODE -ne 0) { throw "не удалось распаковать архив на сервере" }
}
finally {
    Remove-Item $tar -ErrorAction SilentlyContinue
}

Write-Host "==> Копирую отдельные файлы" -ForegroundColor Cyan
foreach ($file in $files) {
    Write-Host "    $file"
    scp -q (Join-Path $root $file) "${Server}:$AppDir/"
    if ($LASTEXITCODE -ne 0) { throw "не удалось скопировать $file" }
}

ssh $Server "chmod +x $AppDir/deploy/bootstrap.sh"

Write-Host ""
Write-Host "Готово. Дальше на сервере:" -ForegroundColor Green
Write-Host "    ssh $Server 'bash $AppDir/deploy/bootstrap.sh'"
