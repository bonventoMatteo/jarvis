<#
.SYNOPSIS
    Instala o JARVIS no Windows 11.

.DESCRIPTION
    1. Verifica o Python 3.11+
    2. Cria o venv (.venv) e instala requirements.txt
    3. Instala as DLLs de CUDA (se houver GPU NVIDIA)
    4. Baixa os modelos: voz piper, faster-whisper, openWakeWord, silero-vad
    5. Instala o Chromium do Playwright
    6. Cria o .env a partir do .env.example e pede a ANTHROPIC_API_KEY
    7. Gera os sons sintéticos (assets/*.wav)
    8. Opcional: cria atalho na pasta Inicializar do Windows

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -SkipModels -NoStartup
#>
[CmdletBinding()]
param(
    [switch]$SkipModels,
    [switch]$SkipCuda,
    [switch]$NoStartup,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok([string]$Message) { Write-Host "    ok: $Message" -ForegroundColor Green }
function Write-Warn([string]$Message) { Write-Host "    aviso: $Message" -ForegroundColor Yellow }

function Invoke-Checked([string]$Exe, [string[]]$Arguments, [string]$What) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$What falhou (código $LASTEXITCODE)." }
}

Write-Host ""
Write-Host "   J.A.R.V.I.S — instalação" -ForegroundColor Cyan
Write-Host "   Just A Rather Very Intelligent System" -ForegroundColor DarkGray

# --------------------------------------------------------------------------
# 1. Python
# --------------------------------------------------------------------------
Write-Step "Procurando Python 3.11+"
$candidates = @()
if ($Python) { $candidates += ,@($Python) }
$candidates += ,@("py", "-3.13")
$candidates += ,@("py", "-3.12")
$candidates += ,@("py", "-3.11")
$candidates += ,@("python")

$PyCmd = $null
foreach ($candidate in $candidates) {
    $exe = $candidate[0]
    $prefix = @()
    if ($candidate.Count -gt 1) { $prefix = @($candidate[1..($candidate.Count - 1)]) }
    try {
        $version = & $exe @prefix -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $version) {
            $parts = $version.Trim().Split(".")
            if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 11) {
                $PyCmd = $candidate
                Write-Ok "Python $version ($($candidate -join ' '))"
                break
            }
        }
    } catch { continue }
}
if (-not $PyCmd) {
    throw "Python 3.11+ não encontrado. Instale em https://www.python.org/downloads/ (marque 'Add to PATH')."
}

# --------------------------------------------------------------------------
# 2. venv + dependências
# --------------------------------------------------------------------------
Write-Step "Criando ambiente virtual (.venv)"
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    $exe = $PyCmd[0]
    $prefix = @()
    if ($PyCmd.Count -gt 1) { $prefix = @($PyCmd[1..($PyCmd.Count - 1)]) }
    Invoke-Checked $exe ($prefix + @("-m", "venv", ".venv")) "Criação do venv"
    Write-Ok ".venv criado"
} else {
    Write-Ok ".venv já existe"
}

Write-Step "Instalando dependências (pode levar alguns minutos)"
Invoke-Checked $VenvPython @("-m", "pip", "install", "--upgrade", "pip", "wheel") "Atualização do pip"
Invoke-Checked $VenvPython @("-m", "pip", "install", "-r", "requirements.txt") "Instalação do requirements.txt"
Write-Ok "pacotes instalados"

# --------------------------------------------------------------------------
# 3. CUDA (opcional)
# --------------------------------------------------------------------------
if (-not $SkipCuda) {
    Write-Step "Verificando GPU NVIDIA"
    $nvidia = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($nvidia) {
        $gpu = "NVIDIA"
        try { $gpu = (& nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1) } catch { }
        Write-Ok "GPU encontrada: $gpu"
        Write-Host "    instalando cuBLAS + cuDNN 9 (CUDA 12) para o faster-whisper..."
        & $VenvPython -m pip install "nvidia-cublas-cu12" "nvidia-cudnn-cu12==9.*"
        if ($LASTEXITCODE -ne 0) { Write-Warn "falhou; o STT vai rodar na CPU (int8)." }
        else { Write-Ok "bibliotecas CUDA instaladas" }
    } else {
        Write-Warn "nenhuma GPU NVIDIA; o STT vai rodar na CPU (int8). Considere WHISPER_MODEL=small no .env."
    }
}

# --------------------------------------------------------------------------
# 4. .env
# --------------------------------------------------------------------------
Write-Step "Configurando .env"
$EnvFile = Join-Path $Root ".env"
if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $Root ".env.example") $EnvFile
    Write-Ok ".env criado a partir do .env.example"
}
$envText = Get-Content $EnvFile -Raw -Encoding UTF8
if ($envText -match "(?m)^ANTHROPIC_API_KEY=sk-ant-\.\.\.\s*$" -or $envText -notmatch "(?m)^ANTHROPIC_API_KEY=sk-") {
    Write-Host "    A chave da Anthropic habilita as tarefas complexas (comandos rápidos funcionam sem ela)."
    Write-Host "    Crie uma em https://console.anthropic.com/settings/keys"
    $secure = Read-Host "    ANTHROPIC_API_KEY (Enter para pular)" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    if ($key) {
        $envText = [regex]::Replace($envText, "(?m)^ANTHROPIC_API_KEY=.*$", "ANTHROPIC_API_KEY=$key")
        [IO.File]::WriteAllText($EnvFile, $envText, (New-Object System.Text.UTF8Encoding($false)))
        Write-Ok "chave gravada no .env"
    } else {
        Write-Warn "sem chave; edite o .env depois."
    }
} else {
    Write-Ok "ANTHROPIC_API_KEY já configurada"
}

# --------------------------------------------------------------------------
# 5. Modelos + sons
# --------------------------------------------------------------------------
if (-not $SkipModels) {
    Write-Step "Baixando modelos (voz, whisper, wake word, VAD) e gerando sons"
    & $VenvPython -m scripts.download_models
    if ($LASTEXITCODE -ne 0) { Write-Warn "alguns modelos falharam; rode 'python -m scripts.download_models' de novo depois." }
} else {
    Write-Step "Gerando sons (modelos pulados)"
    Invoke-Checked $VenvPython @("-m", "scripts.download_models", "--only", "assets") "Geração dos sons"
}

Write-Step "Instalando o Chromium do Playwright"
& $VenvPython -m playwright install chromium
if ($LASTEXITCODE -ne 0) { Write-Warn "falhou; as tarefas de navegador do agente não vão funcionar até 'python -m playwright install chromium'." }
else { Write-Ok "Chromium instalado" }

# --------------------------------------------------------------------------
# 6. Atalhos
# --------------------------------------------------------------------------
$Launcher = Join-Path $Root "jarvis.bat"
$launcherText = "@echo off`r`ncd /d `"%~dp0`"`r`n`".venv\Scripts\python.exe`" main.py %*`r`n"
[IO.File]::WriteAllText($Launcher, $launcherText, (New-Object System.Text.ASCIIEncoding))
Write-Ok "jarvis.bat criado"

if (-not $NoStartup) {
    $answer = Read-Host "`n    Iniciar o JARVIS junto com o Windows? (s/N)"
    if ($answer -match "^[sSyY]") {
        $startup = [Environment]::GetFolderPath("Startup")
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut((Join-Path $startup "JARVIS.lnk"))
        $shortcut.TargetPath = $Launcher
        $shortcut.WorkingDirectory = $Root
        $shortcut.WindowStyle = 7  # minimizado
        $shortcut.Description = "JARVIS — assistente de voz"
        $shortcut.Save()
        Write-Ok "atalho criado em $startup"
    }
}

# --------------------------------------------------------------------------
Write-Host ""
Write-Host "Instalação concluída." -ForegroundColor Green
Write-Host "  Iniciar:            .\jarvis.bat"
Write-Host "  Modo texto:         .\jarvis.bat --text"
Write-Host "  Testar palmas:      .venv\Scripts\python.exe -m scripts.clap_test"
Write-Host "  Dispositivos:       .\jarvis.bat --list-devices"
Write-Host ""
