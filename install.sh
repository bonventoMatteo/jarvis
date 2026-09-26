#!/usr/bin/env bash
# JARVIS — instalação no Ubuntu/Debian (22.04+).
#   ./install.sh               # tudo
#   ./install.sh --skip-models # sem baixar modelos
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
SKIP_MODELS=0; NO_AUTOSTART=0
for arg in "$@"; do
  case "$arg" in
    --skip-models) SKIP_MODELS=1 ;;
    --no-autostart) NO_AUTOSTART=1 ;;
  esac
done

step() { printf '\n\033[36m==> %s\033[0m\n' "$1"; }
ok()   { printf '    \033[32mok:\033[0m %s\n' "$1"; }
warn() { printf '    \033[33maviso:\033[0m %s\n' "$1"; }

step "Pacotes do sistema (sudo)"
PKGS=(python3 python3-venv python3-dev portaudio19-dev libportaudio2 ffmpeg
      wmctrl xdotool playerctl brightnessctl pulseaudio-utils network-manager
      xdg-user-dirs libglib2.0-bin plocate espeak-ng gnome-screenshot scrot
      python3-tk libnotify-bin)
if command -v apt-get >/dev/null; then
  sudo apt-get update -qq
  sudo apt-get install -y "${PKGS[@]}" || warn "alguns pacotes falharam; o JARVIS roda em modo degradado."
  ok "pacotes instalados"
  # brightnessctl sem sudo
  sudo usermod -aG video "$USER" 2>/dev/null && ok "usuário no grupo video (vale no próximo login)"
else
  warn "apt-get não encontrado; instale manualmente: ${PKGS[*]}"
fi

step "Python 3.11+"
PY=""
for cand in python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    PY="$cand"; break
  fi
done
[ -n "$PY" ] || { echo "Python 3.11+ não encontrado (sudo apt install python3.11 python3.11-venv)"; exit 1; }
ok "$($PY --version)"

step "Ambiente virtual e dependências"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip wheel
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install --no-deps -r requirements-nodeps.txt
ok "pacotes Python instalados"

if command -v nvidia-smi >/dev/null; then
  step "GPU NVIDIA detectada: bibliotecas CUDA para o faster-whisper"
  .venv/bin/python -m pip install -q "nvidia-cublas-cu12" "nvidia-cudnn-cu12==9.*" \
    && ok "cuBLAS + cuDNN instalados" || warn "falhou; STT vai rodar na CPU"
fi

step "Configurando .env"
if [ ! -f .env ]; then cp .env.example .env; ok ".env criado"; fi
if ! grep -qE '^ANTHROPIC_API_KEY=sk-ant-[^.]{20,}' .env; then
  echo "    A chave habilita tarefas complexas (https://console.anthropic.com/settings/keys)."
  read -r -s -p "    ANTHROPIC_API_KEY (Enter para pular): " KEY; echo
  if [ -n "${KEY:-}" ]; then
    sed -i "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=${KEY}|" .env && ok "chave gravada"
  else
    warn "sem chave; edite o .env depois"
  fi
fi

if [ "$SKIP_MODELS" -eq 0 ]; then
  step "Modelos (voz, whisper, wake word, VAD) e sons"
  .venv/bin/python -m scripts.download_models || warn "alguns modelos falharam; rode de novo depois"
else
  .venv/bin/python -m scripts.download_models --only assets
fi

step "Chromium do Playwright"
.venv/bin/python -m playwright install --with-deps chromium || warn "falhou; tarefas de navegador do agente indisponíveis"

step "Atalhos"
chmod +x jarvis.sh
DESKTOP_FILE="$HOME/.local/share/applications/jarvis.desktop"
mkdir -p "$(dirname "$DESKTOP_FILE")"
cat > "$DESKTOP_FILE" <<DESK
[Desktop Entry]
Type=Application
Name=JARVIS
Comment=Assistente de voz
Exec=x-terminal-emulator -e "$ROOT/jarvis.sh"
Terminal=false
Categories=Utility;
DESK
ok "atalho no menu de aplicativos"

if [ "$NO_AUTOSTART" -eq 0 ]; then
  read -r -p "    Iniciar o JARVIS junto com o sistema? (s/N) " ANS
  if [[ "${ANS:-n}" =~ ^[sSyY] ]]; then
    mkdir -p "$HOME/.config/autostart"
    cp "$DESKTOP_FILE" "$HOME/.config/autostart/jarvis.desktop"
    ok "autostart configurado"
  fi
fi

if [ "${XDG_SESSION_TYPE:-}" = "wayland" ] && command -v gsettings >/dev/null; then
  step "Wayland: atalho Ctrl+Alt+J pelo GNOME"
  BASE=org.gnome.settings-daemon.plugins.media-keys
  PATH_KEY=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/jarvis/
  CURRENT=$(gsettings get $BASE custom-keybindings)
  if [[ "$CURRENT" != *jarvis* ]]; then
    NEW=$(python3 -c "import ast,sys; l=ast.literal_eval(sys.argv[1].replace('@as ','')); l.append('$PATH_KEY'); print(l)" "$CURRENT")
    gsettings set $BASE custom-keybindings "$NEW"
  fi
  gsettings set $BASE.custom-keybinding:$PATH_KEY name 'JARVIS'
  gsettings set $BASE.custom-keybinding:$PATH_KEY command "$ROOT/jarvis.sh --activate"
  gsettings set $BASE.custom-keybinding:$PATH_KEY binding '<Control><Alt>j'
  ok "Ctrl+Alt+J registrado no GNOME"
fi

printf '\n\033[32mInstalação concluída.\033[0m\n'
echo "  Iniciar:        ./jarvis.sh"
echo "  Modo texto:     ./jarvis.sh --text"
echo "  Testar palmas:  .venv/bin/python -m scripts.clap_test"
