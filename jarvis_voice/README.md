# jarvis_voice — a voz do Jarvis

Módulo de TTS leve, offline e sem GPU: **piper-tts** sintetiza, **pedalboard**
aplica a cadeia "IA de cinema" e **sounddevice** toca. Frases já faladas ficam
em cache (`.npy`), então as falas fixas saem na hora.

## Pré-requisitos

- Python 3.11+
- Windows 11 (também roda em Linux e macOS)
- Saída de áudio funcionando

## Instalação

Dentro da pasta `jarvis_voice/`:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python download_voice.py
python demo.py
```

No Linux/macOS, ative com `source .venv/bin/activate`.

`download_voice.py` tenta primeiro o downloader oficial do piper
(`piper.download_voices`) e, se ele falhar, baixa direto do HuggingFace. Se o
download não passar (proxy corporativo, por exemplo), baixe estes dois arquivos
para `jarvis_voice/models/`:

- https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx
- https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx.json

## Como usar em outro projeto

Com a pasta que contém `jarvis_voice/` no `PYTHONPATH` (ou rodando da raiz do repositório):

```python
from jarvis_voice import JarvisVoice

voice = JarvisVoice()                             # carrega models/pt_BR-faber-medium.onnx
voice.preload()                                   # gera o cache das frases comuns
voice.speak("Sistemas online. Ao seu dispor.")    # bloqueia até terminar
voice.speak("Alerta, senhor.", mood="urgent", block=False)
```

Dentro de código assíncrono:

```python
task = voice.speak_async("Verificando.", mood="confirm")
await task
```

## API

| Método | O que faz |
|---|---|
| `JarvisVoice(model_path=None, cache_dir=None, sample_rate=22050)` | Carrega a voz. Levanta `FileNotFoundError` apontando para `download_voice.py` se o modelo não existir. |
| `speak(text, mood="neutral", block=True)` | Fala. Usa o cache antes de sintetizar. |
| `speak_async(text, mood)` | Devolve uma `asyncio.Task` que fala numa thread. |
| `render(text, mood)` | Só devolve o áudio (`np.float32`), sem tocar. |
| `preload(phrases=None)` | Gera o cache de uma lista de frases (padrão: `COMMON_PHRASES`). |
| `stop()` | Interrompe a reprodução. |
| `clear_cache()` | Apaga o cache em disco e em memória. |

## Moods

| Mood | length_scale | noise_scale | Quando usar |
|---|---|---|---|
| `neutral` | 1.05 | 0.667 | Respostas normais, boot, relatórios. |
| `calm` | 1.15 | 0.500 | Mensagens tranquilas, boa-noite, erros sem gravidade. |
| `urgent` | 0.95 | 0.750 | Alertas, alarmes, avisos que pedem atenção. |
| `confirm` | 1.00 | 0.600 | "Executando.", "Feito." e perguntas de sim/não. |

`length_scale` acima de 1 deixa a fala mais lenta; `noise_scale` controla a
variação de entonação.

## Cadeia de efeitos

```
HighpassFilter 100 Hz → LowpassFilter 8,5 kHz
→ PeakFilter 180 Hz +1,5 dB (q 1,0) → PeakFilter 2,8 kHz +2,5 dB (q 1,4) → PeakFilter 5 kHz +1,0 dB (q 2,0)
→ Compressor −16 dB 3,5:1 (3 ms / 90 ms)
→ Reverb sala 0,12, damping 0,75, wet 0,06, dry 0,94, largura 0,9
→ Gain +2 dB → Limiter −1 dB (80 ms)
```

Para alterar a cadeia, edite `build_chain()` em `engine.py` e troque o valor de
`_CHAIN_VERSION`. Isso invalida o cache antigo.

## Como funciona o cache

A chave é o MD5 de `versão da cadeia | voz | mood | texto`. O áudio já processado
fica em `cache/<hash>.npy`, e as 128 frases mais recentes também ficam em memória.
A partir da segunda execução, uma frase em cache sai em menos de 10 ms.

## Troubleshooting

- **"piper: command not found" / `No module named piper`**: reinstale com
  `pip install --force-reinstall piper-tts` no mesmo venv. O módulo chama
  `python -m piper` com o mesmo interpretador que está rodando.
- **Primeira frase demora**: cada frase nova abre um processo do piper, que
  carrega o modelo. Chame `preload()` no boot para as frases fixas.
- **Som distorcido**: reduza o `Gain` para 0 dB em `build_chain()`.
- **Voz muito grave ou aguda**: ajuste o ganho do `PeakFilter` de 180 Hz
  (negativo deixa a voz mais leve).
- **Acentos saindo errado**: o texto vai para o piper em UTF-8
  (`PYTHONIOENCODING=utf-8`). Confira se o seu código não passa bytes em outra
  codificação.
- **Nada toca**: rode `python -m sounddevice` para listar as saídas e confira o
  dispositivo padrão do Windows.

## Consumo estimado

- RAM: menos de 200 MB (processo do piper + modelo *medium* de ~60 MB).
- CPU: um núcleo durante a síntese, só em frases novas. Tocar uma frase do cache
  não gasta quase nada.
- Latência: frase em cache abaixo de 10 ms. Frase nova depende da CPU e do tamanho
  do texto, porque cada síntese inicia um processo do piper e carrega o modelo.
  Por isso as frases fixas devem ser pré-carregadas.

## Como trocar a voz

1. Escolha uma voz em https://huggingface.co/rhasspy/piper-voices (ex.: `pt_BR-edresson-low`).
2. Baixe com `python -c "from download_voice import main; main('pt_BR-edresson-low')"`
   ou coloque o `.onnx` e o `.onnx.json` em `models/`.
3. Mude `DEFAULT_MODEL` em `config.py`, ou passe `JarvisVoice(model_path="models/pt_BR-edresson-low.onnx")`.

A taxa de amostragem é lida do `.onnx.json`. Vozes *low* usam 16 kHz e funcionam
sem nenhum ajuste.
