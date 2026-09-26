# J.A.R.V.I.S

Assistente de voz para **Ubuntu** e **Windows 11**. Você bate duas palmas (ou diz "hey Jarvis", ou aperta
Ctrl+Alt+J), ele responde "Sim?" e executa o pedido no seu computador de verdade.

- **Ativação:** palma dupla (com calibração do ruído da sala), wake word openWakeWord, atalho global
  ou gatilho externo (`jarvis.sh --activate`).
- **Ouvido:** silero-vad detecta o fim da fala e o faster-whisper transcreve (usa CUDA se houver GPU).
- **Cérebro:** 73 comandos em português resolvidos por regex, sem LLM e em milissegundos. O que não casa
  vai para o Claude Haiku classificar, e as tarefas complexas vão para o Claude Sonnet com 24 ferramentas
  (shell, apps, janelas, arquivos, navegador, visão de tela, agenda…).
- **Voz:** piper `pt_BR-faber-medium` com cadeia pedalboard (EQ, compressor, reverb curto) para soar
  como uma IA de filme. Também existe o pacote separado [`jarvis_voice/`](jarvis_voice/README.md).
- **Clima de filme:** som de boot, "bwoom" na ativação, hum eletrônico enquanto pensa, bipes de
  confirmação e erro, LED ciano piscando no painel.
- **Painel:** rich no terminal, com estado, forma de onda do microfone, turno atual, últimos comandos e
  latência.

## Sumário

- [Pré-requisitos](#pré-requisitos)
- [Instalação no Ubuntu](#instalação-no-ubuntu)
- [Instalação no Windows 11](#instalação-no-windows-11)
- [Uso](#uso)
- [Como calibrar as palmas](#como-calibrar-as-palmas)
- [Wake word customizado ("jarvis")](#wake-word-customizado-jarvis)
- [Comandos rápidos](#comandos-rápidos)
- [Tarefas complexas (agente)](#tarefas-complexas-agente)
- [Arquitetura](#arquitetura)
- [Troubleshooting](#troubleshooting)
- [Desenvolvimento](#desenvolvimento)

## Pré-requisitos

| Item | Detalhe |
|---|---|
| Sistema | Ubuntu 22.04+ (GNOME, KDE ou XFCE; **Xorg recomendado**) ou Windows 11 |
| Python | 3.11 ou mais novo |
| Microfone | qualquer um; um microfone de mesa capta melhor as palmas |
| GPU (opcional) | NVIDIA com driver recente. O instalador adiciona cuBLAS e cuDNN, e sem GPU o STT roda na CPU (int8) |
| Chave da API | [console.anthropic.com](https://console.anthropic.com/settings/keys). Sem ela, só os comandos rápidos funcionam |
| Disco | ~4 GB (torch, modelos do whisper e da voz) |

## Instalação no Ubuntu

```bash
git clone https://github.com/bonventoMatteo/jarvis.git
cd jarvis
./install.sh
./jarvis.sh
```

O `install.sh`:

1. instala via apt `portaudio`, `wmctrl`, `xdotool`, `playerctl`, `brightnessctl`, `pulseaudio-utils`,
   `plocate`, `espeak-ng`, `gnome-screenshot` e outros, e coloca você no grupo `video` (para o brilho);
2. cria o `.venv` e instala o `requirements.txt` (e as bibliotecas CUDA, se houver GPU NVIDIA);
3. cria o `.env` e pede a `ANTHROPIC_API_KEY`;
4. baixa os modelos (voz, whisper, wake word, VAD), gera os sons e instala o Chromium do Playwright;
5. cria um atalho no menu de aplicativos e, se você quiser, inicia o JARVIS junto com o sistema;
6. **no Wayland**, registra Ctrl+Alt+J como atalho do GNOME apontando para `jarvis.sh --activate`.

### Xorg ou Wayland?

O Ubuntu usa Wayland por padrão. Nele, os aplicativos não podem ler teclas globais nem mexer em
janelas de outros apps. O que muda:

| Recurso | Xorg | Wayland |
|---|---|---|
| Palmas, wake word, voz, agente | ✅ | ✅ |
| Ctrl+Alt+J | ✅ (pynput) | ✅ via atalho do GNOME (o `install.sh` configura) |
| Controlar janela pelo nome | ✅ (wmctrl) | ⚠️ só apps XWayland; senão, só a janela em foco |
| Digitar/clicar (`type_text`, `click_element`) | ✅ | ⚠️ só em apps XWayland |
| Print de tela | ✅ | ✅ (gnome-screenshot/grim) |

Para ter tudo, escolha **"Ubuntu no Xorg"** na engrenagem da tela de login.

## Instalação no Windows 11

```powershell
git clone https://github.com/bonventoMatteo/jarvis.git
cd jarvis
powershell -ExecutionPolicy Bypass -File .\install.ps1
.\jarvis.bat
```

O `install.ps1` faz o mesmo que o `install.sh`: venv, dependências, CUDA, `.env`, modelos, Chromium,
`jarvis.bat` e, se você quiser, atalho na pasta Inicializar.

### Instalação manual (qualquer sistema)

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install --no-deps -r requirements-nodeps.txt
cp .env.example .env               # e edite a ANTHROPIC_API_KEY
python -m scripts.download_models
python -m playwright install chromium
python main.py
```

## Uso

```bash
./jarvis.sh                    # completo: palmas + wake word + atalho + painel
./jarvis.sh --text             # digite comandos (sem microfone), bom para testar
./jarvis.sh --no-dashboard     # logs no console em vez do painel
./jarvis.sh --list-devices     # microfones e saídas (use os índices no .env)
./jarvis.sh --no-clap --no-wake --skip-calibration --mute --debug

./jarvis.sh --activate         # ativa um JARVIS que já está rodando
./jarvis.sh --send "volume 30" # manda um comando de texto a ele
```

No Windows, use `jarvis.bat` com os mesmos argumentos.

**Um turno completo:**

1. Palma dupla. O LED do painel pisca em ciano, toca o "bwoom" e o Jarvis diz "Sim?" (ou "Pronto",
   "Escutando", "Diga"…).
2. Você fala. O silero-vad corta no silêncio (800 ms por padrão).
3. O hum de processamento toca enquanto o faster-whisper transcreve.
4. O roteador decide o caminho:
   - regex: executa na hora;
   - Haiku: classifica a frase;
   - Sonnet: usa as ferramentas e narra o que está fazendo ("Abrindo o VS Code...").
5. Um bipe de sucesso ou erro e a resposta em uma frase. Se algo falhar, ele diz o motivo real:
   "Desculpe, senhor, não consegui: …".

Ações destrutivas (desligar, reiniciar, apagar arquivo, esvaziar a lixeira, desligar o Wi-Fi, comandos
de shell perigosos) pedem confirmação por voz. Desligar e reiniciar pedem duas vezes ("diga
*confirmo*"). Bater palmas ou usar o atalho enquanto ele fala interrompe a fala.

Timers, alarmes e lembretes ficam salvos em `data/schedule.json` e sobrevivem a um reinício.

## Como calibrar as palmas

Ao iniciar, o JARVIS escuta **5 segundos de silêncio** para medir o ruído da sala. Fique quieto
nesse tempo. Uma palma só é aceita quando:

1. o pico passa de `CLAP_THRESHOLD_MULT` × ruído (padrão 8×, com piso `CLAP_MIN_PEAK`);
2. o ataque (do início ao pico, pelo envelope de Hilbert) dura menos de `CLAP_ATTACK_MS` (15 ms) e o som
   decai rápido;
3. pelo menos `CLAP_BAND_RATIO` (30%) da energia fica entre 2 e 4 kHz (voz e batidas na mesa são graves).

Duas palmas separadas por 150 a 1200 ms ativam o Jarvis.

Para ver os números ao vivo:

```bash
.venv/bin/python -m scripts.clap_test
```

Cada candidato mostra pico, ataque, decaimento, fração da banda e por que foi aceito ou rejeitado.
Ajuste no `.env`:

| Sintoma | Ajuste |
|---|---|
| Não reconhece as palmas | `CLAP_THRESHOLD_MULT=6`, `CLAP_BAND_RATIO=0.22` |
| Dispara sozinho (teclado, porta) | `CLAP_THRESHOLD_MULT=11`, `CLAP_ATTACK_MS=10` |
| Palmas lentas demais | `CLAP_MAX_GAP_MS=1500` |
| Quer três palmas | `CLAP_REQUIRED=3` |

O painel também mostra em ciano as barras da forma de onda que passam do limiar.

## Wake word customizado ("jarvis")

O padrão é o modelo pré-treinado **"hey jarvis"** do openWakeWord. Para usar só "jarvis":

1. Abra o notebook de treino automático do openWakeWord
   ([github.com/dscripka/openWakeWord](https://github.com/dscripka/openWakeWord), seção *Training New
   Models*) no Google Colab.
2. Defina `target_word = "jarvis"` e rode tudo (uns 30 a 60 minutos em GPU gratuita).
3. Baixe o `jarvis.onnx` e coloque em `models/openwakeword/jarvis.onnx`.
4. No `.env`, defina `WAKE_MODEL=jarvis.onnx` e ajuste `WAKE_THRESHOLD` (comece em 0.5). Veja o score
   no painel.

## Comandos rápidos

Resolvidos por regex, sem LLM e sem rede. Os exemplos são ilustrativos: há variações, e "Jarvis" ou "por
favor" no começo e no fim são ignorados.

| Categoria | Exemplos |
|---|---|
| Apps | "abrir o Chrome", "abre o Spotify", "fecha o Discord", "inicia o VS Code", "vai para o Firefox" |
| Janelas | "minimizar tudo", "mostrar a área de trabalho", "alt tab", "fechar janela", "maximizar", "tela cheia", "quais janelas estão abertas" |
| Sistema | "bloquear o computador", "coloca o PC pra dormir", "reiniciar o computador"², "desligar o PC"², "cancela o desligamento", "gerenciador de tarefas", "prompt de comando como administrador" |
| Mídia | "pausa a música", "play", "próxima música", "música anterior", "para a música" |
| Volume | "volume 50", "volume no máximo", "deixa o volume em trinta", "aumenta o volume", "abaixa o som", "mudo", "tira do mudo", "qual é o volume" |
| Tela | "tira um print", "print de uma região", "aumenta o brilho", "brilho em 70 por cento" |
| Navegador | "nova aba", "fecha a aba", "próxima aba", "aba anterior", "reabre a aba", "recarregar", "histórico", "downloads do navegador" |
| Web | "pesquisa no Google receita de bolo", "toca Daft Punk no YouTube", "abrir o Gmail", "abrir o GitHub", "abre o site wikipedia.org" |
| Arquivos | "abrir a pasta downloads", "abre os documentos", "criar pasta Projetos aqui", "buscar arquivo contrato.pdf", "abre o arquivo orçamento", "apagar o arquivo rascunho.txt"¹, "esvaziar a lixeira"¹ |
| Info | "que horas são", "que dia é hoje", "clima em São Paulo", "como está o tempo", "status do sistema" |
| Agenda | "timer de 5 minutos", "cancela o timer", "alarme para 7:30", "me acorda às 6 e meia", "me lembra de ligar pro João em 20 minutos", "lembre-me de tomar remédio às 22h", "quanto falta" |
| Edição | "copiar", "colar", "recortar", "desfazer", "refazer", "selecionar tudo", "salvar", "novo documento", "o que tem na área de transferência" |
| Rede | "liga o wi-fi", "desligar o wi-fi"¹ |
| Controle | "cancela", "repete", "desligar o Jarvis"¹ |

¹ pede confirmação por voz. ² pede confirmação dupla.

## Tarefas complexas (agente)

Tudo o que não é comando rápido vai para o Sonnet com ferramentas. Exemplos:

- "Abre o VS Code no projeto jarvis e roda os testes."
- "Qual a cotação do dólar hoje?" (pesquisa na web e resume)
- "Clica no botão de enviar." (tira um print, localiza com visão e clica)
- "Cria um arquivo notas.md na área de trabalho com a lista de compras: pão, leite e café."
- "Às 18h abre o Spotify." (agenda um comando)
- "Lembra que meu projeto principal fica em ~/dev/jarvis." (memória permanente)

As 24 ferramentas estão em [`llm/tools.py`](llm/tools.py): `execute_shell`, `open_application`,
`close_application`, `control_window`, `list_windows`, `type_text`, `send_hotkey`, `click_at`,
`click_element`, `read_file`, `write_file`, `search_files`, `list_folder`, `browser_navigate`,
`browser_search_and_extract`, `browser_read_page`, `browser_action`, `take_screenshot`,
`get_system_info`, `media_control`, `schedule_task`, `remember_fact`, `speak` e `ask_confirmation`.
Comandos de shell que parecem destrutivos (`rm -r`, `del /s`, `format`, `shutdown`…) sempre pedem
confirmação antes.

## Arquitetura

```
main.py                     entrypoint (argparse, painel ou modo texto, --activate/--send)
config.py                   pydantic-settings (.env)
core/events.py              event bus asyncio com fan-out (thread-safe)
core/state.py               FSM: BOOTING → CALIBRATING → IDLE → LISTENING → THINKING → EXECUTING → SPEAKING
core/orchestrator.py        coordena tudo; trace_id por turno (structlog)
core/scheduler.py           timers, alarmes, lembretes, tarefas agendadas (JSON persistente)
core/ipc.py                 gatilho externo em 127.0.0.1 (ACTIVATE / TEXT)
audio/mic.py                captura contínua 16 kHz, buffer circular, fan-out
audio/clap.py               detector de palmas (Hilbert + FFT + limiar adaptativo)
audio/wake.py               openWakeWord
audio/vad.py                silero-vad (com fallback de energia)
audio/hotkey.py             atalho global (keyboard no Windows, pynput no Linux)
audio/player.py             mixer de efeitos + trilha ambiente em loop
stt/whisper_engine.py       faster-whisper (CUDA automático, fallback CPU int8)
llm/router.py               regex primeiro, Haiku depois
llm/agent.py                Sonnet + tool use + streaming + visão (click_element)
llm/tools.py                schemas JSON + executor das ferramentas
llm/prompts.py              persona e prompts
tts/piper_engine.py         piper + cache de frases + moods (fallback espeak-ng/SAPI)
tts/effects.py              cadeia pedalboard
tts/voice_lines.py          falas variadas por estado
executor/pc.py              PCController (Windows e Linux)
executor/linux.py           wmctrl, xdotool, pactl, playerctl, brightnessctl, nmcli, gio, plocate…
executor/commands.py        execução dos comandos rápidos
executor/apps.py, files.py, media.py, browser.py
memory/store.py             SQLite (histórico e fatos) + JSON (preferências)
ui/dashboard.py             painel rich Live
assets/generate_assets.py   sons sintéticos (senoides + ADSR), gerados se faltarem
jarvis_voice/               módulo de voz independente
scripts/                    download_models, audio_devices, clap_test
tests/                      170 testes (pytest)
```

**Sobre o STT:** o `distil-large-v3` só transcreve inglês. Por isso o padrão é o `large-v3-turbo`, que é
multilíngue e rápido. Em CPU fraca, use `WHISPER_MODEL=small`.

## Troubleshooting

**Microfone não detectado / "Não consegui abrir o microfone"**
- Rode `./jarvis.sh --list-devices` e ponha o índice certo em `INPUT_DEVICE` no `.env`.
- Ubuntu: confira em Configurações → Som → Entrada. Veja também se o `libportaudio2` está instalado.
- Windows: libere em Configurações → Privacidade → Microfone → "Permitir que apps da área de trabalho…".
- Para ver o nível ao vivo: `python -m scripts.audio_devices --meter`.

**CUDA não é usado (painel mostra `cpu/int8`)**
- Confira se `nvidia-smi` funciona e se o driver é recente.
- Instale as bibliotecas: `pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"` (os instaladores já fazem isso).
- Force com `WHISPER_DEVICE=cuda`. O log `stt.cuda_failed` mostra o motivo real.

**Latência alta**
- CPU sem GPU: `WHISPER_MODEL=small` (ou `medium`) e `WHISPER_BEAM_SIZE=1`.
- Fala cortada ou esperando demais: ajuste `VAD_SILENCE_MS` (600 a 1000).
- O painel mostra a latência separada em STT, rota e execução. Comandos rápidos gastam só alguns ms de
  rota, então a espera costuma vir do STT ou do agente.

**Palmas: veja a [seção de calibração](#como-calibrar-as-palmas).**

**Sem voz / voz robótica**
- O painel mostra `voz: none` ou `espeak`: falta o modelo piper. Rode `python -m scripts.download_models --only piper`.
- Voz distorcida: `TTS_GAIN_DB=0`.

**Linux: janelas, digitação ou atalho não funcionam**
- Você provavelmente está no Wayland. Veja [Xorg ou Wayland?](#xorg-ou-wayland).
- O aviso de "ferramentas ausentes" ao iniciar diz o que falta instalar.
- Brilho: faça logout e login depois do `install.sh`, porque o grupo `video` só vale na nova sessão.

**"a chave da API Anthropic não está configurada"**: ponha `ANTHROPIC_API_KEY=sk-ant-...` no `.env`.

**Logs:** `logs/jarvis.jsonl` (JSON por linha, com `trace_id` por comando).

## Desenvolvimento

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest          # 170 testes
.venv/bin/ruff check .
./jarvis.sh --text --mute           # testa o fluxo completo sem microfone nem áudio
```
