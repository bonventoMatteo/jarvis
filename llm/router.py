"""
Roteador de comandos: regex primeiro, Haiku depois, Sonnet por último.

1. **Regex** (`FAST_COMMANDS`, 70+ padrões em pt-BR) — resposta em
   milissegundos, sem rede. Cobre abrir/fechar apps, janelas, mídia, volume,
   brilho, navegador, pastas, timers, alarmes, lembretes, clipboard, energia.
2. **Haiku** (`claude-haiku-4-5`) — se nenhum padrão casar, classifica a
   frase num JSON `{action, target, needs_agent}`. Quando a ação cabe num
   comando rápido, é convertida num `Intent` e executada sem o agente.
3. **Agente** (`claude-sonnet-4-5`) — tudo o que sobra (`route()` devolve
   `None`).

A normalização remove acentos *preservando o comprimento* da string, então
os spans dos grupos capturados no texto sem acento são usados para recortar
o texto original (com acento) — "abrir a pasta Músicas" captura "Músicas".
"""
from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import structlog

from config import settings
from llm.prompts import router_system_prompt

log = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- #
# Normalização
# --------------------------------------------------------------------------- #
def _fold_char(char: str) -> str:
    """Remove o acento de um caractere mantendo exatamente 1 caractere."""
    decomposed = unicodedata.normalize("NFD", char)
    base = decomposed[0] if decomposed else char
    if len(base) != 1:
        return char
    if not (base.isalnum() or base in " :/.-%'+_"):
        return " "
    return base


def fold(text: str) -> str:
    """Minúsculas, sem acento, pontuação vira espaço — mesmo comprimento."""
    lowered = text.lower()
    folded = "".join(_fold_char(char) for char in lowered)
    assert len(folded) == len(lowered)
    return folded


_PREFIX_RE = re.compile(
    r"^\s*(?:(?:ok|ei|hey|e ai|oi|ola)\s+)?(?:jarvis\s*)?(?:(?:por favor|poderia|pode|voce pode|consegue|quero que voce|preciso que voce|me faz um favor)\s+)?"
)
# "desligar o jarvis" não pode perder o "jarvis" (lookbehind do artigo).
_SUFFIX_RE = re.compile(r"\s*(?:por favor|pra mim|para mim|(?<!\bo )(?<!\bao )jarvis|obrigado|valeu)?[\s.!?]*$")


def _trim_span(folded: str) -> tuple[int, int]:
    """Span útil da frase, sem saudação/"por favor" nas pontas."""
    start = _PREFIX_RE.match(folded)
    begin = start.end() if start else 0
    end_match = _SUFFIX_RE.search(folded, begin)
    end = end_match.start() if end_match else len(folded)
    return begin, max(begin, end)


# --------------------------------------------------------------------------- #
# Intent
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Intent:
    """Um comando reconhecido e pronto para executar."""

    name: str
    text: str
    params: dict[str, str] = field(default_factory=dict)
    source: str = "regex"
    say_before: str = ""
    speak_result: bool = False
    confirm: str = ""
    double_confirm: bool = False

    @property
    def is_fast(self) -> bool:
        return True

    def describe(self) -> str:
        args = ", ".join(f"{key}={value}" for key, value in self.params.items() if value)
        return f"{self.name}({args})" if args else self.name


@dataclass(frozen=True, slots=True)
class FastCommand:
    """Definição de um comando rápido."""

    name: str
    patterns: tuple[re.Pattern[str], ...]
    say_before: str = ""
    speak_result: bool = False
    confirm: str = ""
    double_confirm: bool = False
    max_words: int = 0  # 0 = sem limite; limita capturas genéricas


def _cmd(
    name: str,
    *patterns: str,
    say: str = "",
    speak: bool = False,
    confirm: str = "",
    double: bool = False,
    max_words: int = 0,
) -> FastCommand:
    return FastCommand(
        name=name,
        patterns=tuple(re.compile(rf"^(?:{p})$") for p in patterns),
        say_before=say,
        speak_result=speak,
        confirm=confirm,
        double_confirm=double,
        max_words=max_words,
    )


# Fragmentos reutilizados (sempre sobre texto SEM acento).
_OPEN = r"(?:abr\w*|abra|inici\w+|execut\w+|lanc\w+|rod\w+|carreg\w+)"
_CLOSE = r"(?:fech\w+|encerr\w+|mat\w+|finaliz\w+|sai\w* d[oae])"
_ART = r"(?:(?:o|a|os|as|um|uma|meu|minha|em|no|na)\s+)?"
_SET = r"(?:coloc\w+|poe|por|ponha|bot\w+|ajust\w+|deix\w+|mud\w+|defin\w+|seta\w*|altera\w*)"
_UP = r"(?:aument\w+|sob\w+|subir|suba|mais|levant\w+)"
_DOWN = r"(?:diminu\w+|abaix\w+|baix\w+|reduz\w*|menos)"
_NUM = r"(?P<n>\d{1,3}|[a-z]+(?: e [a-z]+)?)"
_UNIT = r"(?P<unit>segundos?|seg|minutos?|min|horas?)"
_SEARCH = r"(?:pesquis\w+|busc\w+|procur\w+)"

#: Ordem importa: padrões específicos antes dos genéricos (abrir/fechar X).
FAST_COMMANDS: tuple[FastCommand, ...] = (
    # ---------------------------- controle ------------------------------- #
    _cmd("cancel", r"cancel\w*|esquece|deixa pra la|nada|nao e nada|nao era nada|deixa|ignora|obrigad\w*|valeu|(?:e |era )?so isso"),
    _cmd("repeat", r"repet\w+(?: (?:isso|o que (?:voce )?disse))?|o que (?:voce )?disse|nao ouvi"),
    _cmd(
        "quit",
        r"(?:encerr\w+|deslig\w+|finaliz\w+|fech\w+) (?:o )?(?:jarvis|assistente|sistema jarvis)|jarvis (?:desligar|offline)|pode (?:se )?desligar",
        confirm="Deseja mesmo me desligar, {title}?",
    ),
    # ------------------------------ info -------------------------------- #
    _cmd("time", r"que horas? (?:sao|e|tem)(?: agora)?|me (?:diz|diga|fala) (?:as|a) horas?|hora(?:s)? atual|horas?|qual (?:e )?a hora", speak=True),
    _cmd(
        "date",
        r"que dia (?:e|eh) hoje|(?:qual|que) (?:e )?a data(?: de hoje)?|data de hoje|que data e hoje|(?:que )?dia de hoje|hoje e que dia",
        speak=True,
    ),
    _cmd(
        "weather",
        r"(?:como (?:esta|ta|vai) )?(?:o )?(?:clima|tempo|previsao(?: do tempo)?|temperatura)(?: agora| hoje)? (?:em|no|na|de|para|pra) (?P<city>.+)",
        r"(?:como (?:esta|ta) )?(?:o )?(?:clima|tempo|previsao(?: do tempo)?|temperatura)(?: agora| hoje| la fora)?|vai chover(?: hoje)?|esta (?:frio|calor)(?: la fora)?",
        speak=True,
    ),
    _cmd(
        "system_info",
        r"(?:status|estado|informac\w+|info|diagnostico) do (?:sistema|pc|computador)|uso de (?:cpu|memoria|processador)|como (?:esta|ta) o (?:pc|computador|sistema)|relatorio do sistema",
        speak=True,
    ),
    _cmd("list_windows", r"quais (?:janelas|programas|apps|aplicativos) (?:estao |tem )?abert\w+", speak=True),
    # ----------------------------- energia ------------------------------ #
    _cmd("lock", r"bloque\w+(?: (?:o|a))?(?: (?:pc|computador|tela|sistema|windows|maquina))?|trav\w+ (?:o |a )?(?:pc|computador|tela)"),
    _cmd(
        "sleep",
        r"(?:suspend\w+|hibern\w+)(?: (?:o )?(?:pc|computador|sistema))?|(?:coloc\w+|poe|bota) (?:o )?(?:pc|computador) (?:pra|para) dormir|(?:pc|computador) dormir|modo (?:de )?suspensao|dormir",
        say="Suspendendo o sistema.",
        confirm="Suspender o computador, {title}?",
    ),
    _cmd(
        "restart",
        r"reinici\w+ (?:o )?(?:pc|computador|sistema|windows|maquina)|reboot",
        confirm="Reiniciar o computador, {title}?",
        double=True,
    ),
    _cmd(
        "shutdown",
        r"deslig\w+ (?:o )?(?:pc|computador|sistema|windows|maquina)|desligar|desligue",
        confirm="Desligar o computador, {title}?",
        double=True,
    ),
    _cmd("cancel_shutdown", r"cancel\w+ (?:o )?(?:desligamento|reinicio|reinicializacao)|nao deslig\w+"),
    _cmd("task_manager", rf"(?:{_OPEN} )?(?:o )?gerenciador de tarefas|task manager", say="Gerenciador de tarefas."),
    _cmd(
        "admin_terminal",
        rf"(?:{_OPEN} )?(?:o |um )?(?P<shell>cmd|prompt(?: de comando)?|terminal|powershell|power shell) (?:como|de|em modo) (?:admin|administrador)",
        say="Abrindo terminal elevado. Confirme na tela.",
    ),
    # ----------------------------- janelas ------------------------------ #
    _cmd("minimize_all", r"minimiz\w+ (?:tudo|todas(?: as janelas)?)"),
    _cmd("show_desktop", r"mostr\w+ (?:a )?(?:area de trabalho|desktop)|(?:ir|vai|volta) (?:para|pra) (?:a )?(?:area de trabalho|desktop)"),
    _cmd("alt_tab", r"alt tab|proxima janela|janela anterior|(?:troc\w+|altern\w+|mud\w+) (?:de )?janela"),
    _cmd("close_window", rf"{_CLOSE} (?:essa |esta |a )?janela(?: atual)?"),
    _cmd("minimize_window", r"minimiz\w+(?: (?:essa|esta|a) janela| janela)?"),
    _cmd("maximize_window", r"maximiz\w+(?: (?:essa|esta|a) janela| janela)?"),
    _cmd("fullscreen", r"(?:modo )?tela cheia|(?:entr\w+|sai\w*) (?:da |de |em )?tela cheia"),
    # ------------------------------ mídia -------------------------------- #
    _cmd("next_track", r"proxim\w+ (?:musica|faixa|cancao|video)|(?:pul\w+|avanc\w+|passa\w*) (?:a |essa |esta )?(?:musica|faixa|cancao)|next"),
    _cmd("prev_track", r"(?:musica|faixa|cancao|video) anterior|volt\w+ (?:a )?(?:musica|faixa|cancao)|previous"),
    _cmd("stop_media", r"par\w+ (?:a )?(?:musica|midia|reproducao|video)|stop"),
    _cmd(
        "play_pause",
        r"(?:da |dar )?(?:play|pause|pausa\w*|despausa\w*|continu\w+|retom\w+)(?: (?:a |o )?(?:musica|video|midia|som))?|(?:toc\w+|solt\w+) (?:a |uma )?musica",
    ),
    # ------------------------------ volume ------------------------------- #
    _cmd("mute", r"(?:modo )?mudo|silenci\w+|mut\w+|tir\w+ o som|sem som"),
    _cmd("unmute", r"desmut\w+|tir\w+ (?:do )?mudo|volt\w+ (?:com )?o som|(?:ativ\w+|reativ\w+|lig\w+) o som"),
    _cmd("volume_get", r"qual (?:e )?o volume(?: atual)?|volume atual", speak=True),
    _cmd("volume_up", rf"{_UP} (?:o )?(?:volume|som)(?: (?:um pouco|mais))?|volume (?:mais alto|pra cima|para cima|\+)|mais alto", speak=True),
    _cmd("volume_down", rf"{_DOWN} (?:o )?(?:volume|som)(?: (?:um pouco|mais))?|volume (?:mais baixo|pra baixo|para baixo|-)|mais baixo", speak=True),
    _cmd(
        "volume_set",
        rf"(?:{_SET} )?(?:o )?volume (?:em |para |pra |no |a |ao )?(?:(?P<level>maximo|minimo|metade|meio)|{_NUM})(?: por cento| %|%)?",
        speak=True,
    ),
    # ------------------------------ brilho ------------------------------- #
    _cmd("brightness_up", rf"{_UP} (?:o )?brilho(?: (?:um pouco|mais))?|mais brilho|brilho (?:mais alto|pra cima)", speak=True),
    _cmd("brightness_down", rf"{_DOWN} (?:o )?brilho(?: (?:um pouco|mais))?|menos brilho|brilho (?:mais baixo|pra baixo)", speak=True),
    _cmd(
        "brightness_set",
        rf"(?:{_SET} )?(?:o )?brilho (?:em |para |pra |no |a |ao )?(?:(?P<level>maximo|minimo|metade|meio)|{_NUM})(?: por cento| %|%)?",
        speak=True,
    ),
    # ------------------------------- tela -------------------------------- #
    _cmd(
        "snip",
        r"(?:print|captura|recorte|screenshot) (?:de |da )?(?:uma )?(?:regiao|area|parte)(?: da tela)?|recort\w+ (?:a )?tela",
        say="Selecione a região.",
    ),
    _cmd(
        "screenshot",
        r"(?:tir\w+|faz\w*|bat\w+|pega\w*) (?:um |uma )?(?:print|captura|screenshot|foto)(?: (?:da|de) tela)?|print(?: (?:da|de) tela)?|captur\w+ (?:a )?tela|screenshot",
        speak=True,
    ),
    # ----------------------------- navegador ----------------------------- #
    _cmd("reopen_tab", r"reabr\w+ (?:a )?(?:ultima )?(?:aba|guia)(?: fechada)?"),
    _cmd("new_tab", rf"(?:{_OPEN} )?(?:uma )?nova (?:aba|guia)"),
    _cmd("close_tab", rf"{_CLOSE} (?:a |essa |esta )?(?:aba|guia)(?: atual)?"),
    _cmd("next_tab", r"proxima (?:aba|guia)"),
    _cmd("prev_tab", r"(?:aba|guia) anterior"),
    _cmd("reload", r"recarreg\w+(?: (?:a )?pagina)?|atualiz\w+ (?:a )?pagina|refresh|f5"),
    _cmd("history", rf"(?:{_OPEN} )?(?:o )?historico(?: do navegador)?"),
    _cmd("browser_downloads", rf"(?:{_OPEN} )?(?:os )?downloads do (?:navegador|chrome|edge)"),
    _cmd(
        "google",
        rf"{_SEARCH} (?:no|na) google (?:por |sobre )?(?P<q>.+)",
        rf"{_SEARCH} (?:por |sobre )?(?P<q>.+?) no google",
        r"googl\w+ (?P<q>.+)",
        rf"{_SEARCH} (?:na internet|na web) (?:por |sobre )?(?P<q>.+)",
        say="Pesquisando.",
    ),
    _cmd(
        "youtube",
        rf"(?:{_SEARCH}|toc\w+|coloc\w+|bot\w+|abr\w+) (?P<q>.+?) no youtube",
        rf"(?:{_OPEN} )?(?:o )?youtube (?:e )?(?:(?:busc\w+|procur\w+|toc\w+|pesquis\w+|com) )(?P<q>.+)",
        rf"(?:{_OPEN} |toc\w+ |coloc\w+ )?(?:o )?youtube",
        say="Abrindo o YouTube.",
    ),
    _cmd("gmail", rf"(?:{_OPEN} |ver |le\w* )?(?:o )?(?:gmail|meu e ?mail|meus e ?mails|caixa de entrada)", say="Abrindo o Gmail."),
    _cmd("github", rf"(?:{_OPEN} )?(?:o )?git ?hub", say="Abrindo o GitHub."),
    _cmd(
        "open_url",
        rf"(?:{_OPEN}|entr\w+ (?:no|em|na)|acess\w+|vai (?:para|pro|no|pra)|ir para)(?: o site| a pagina)? (?P<url>[a-z0-9][\w.-]*\.(?:com|br|net|org|io|dev|gov|ai|app|me|tv)(?:\.br)?(?:/\S*)?)",
        say="Abrindo.",
    ),
    # ----------------------------- arquivos ------------------------------ #
    _cmd(
        "create_folder",
        r"cri\w+ (?:uma )?(?:nova )?pasta(?: chamada| com o nome| de nome)? (?P<name>.+?)(?: (?P<here>aqui)| (?:na|no|em|dentro de|dentro da|dentro do) (?P<parent>.+))?",
        r"cri\w+ (?:uma )?(?:nova )?pasta(?: (?P<here>aqui))?",
        speak=True,
    ),
    _cmd(
        "search_file",
        r"(?:busc\w+|procur\w+|ach\w+|encontr\w+|localiz\w+) (?:o |um |meu )?arquivo(?: chamado| com (?:o )?nome| de nome)? (?P<q>.+)",
        say="Procurando.",
        speak=True,
    ),
    _cmd(
        "delete_file",
        r"(?:delet\w+|apag\w+|exclu\w+|remov\w+) (?:o )?arquivo(?: chamado)? (?P<q>.+)",
        confirm="Mover {q} para a lixeira, {title}?",
    ),
    _cmd(
        "empty_trash",
        r"(?:esvazi\w+|limp\w+) (?:a )?lixeira",
        confirm="Esvaziar a lixeira? Isso não pode ser desfeito.",
    ),
    _cmd(
        "open_folder",
        rf"(?:{_OPEN} |mostr\w+ |ir (?:para|pra) )?(?:a )?pasta (?:de |do |da |dos |das )?(?P<folder>.+)",
        rf"(?:{_OPEN}|mostr\w+) (?:os |as |a |o |meus |minhas )?(?P<folder>downloads|documentos|area de trabalho|imagens|fotos|videos|musicas?)",
        max_words=4,
    ),
    _cmd("open_file", rf"{_OPEN} (?:o )?arquivo(?: chamado)? (?P<q>.+)", say="Um momento."),
    # ------------------------- timers e lembretes ------------------------ #
    _cmd("cancel_timer", r"cancel\w+ (?:o |os |todos os )?(?:timers?|temporizador\w*|cronometro)"),
    _cmd("cancel_alarm", r"cancel\w+ (?:o |os |todos os )?(?:alarmes?|despertador)"),
    _cmd("cancel_reminder", r"cancel\w+ (?:o |os |todos os )?lembretes?"),
    _cmd(
        "list_schedule",
        r"(?:quanto (?:tempo )?falta|quanto falta)(?: (?:no|do|pro|para o) (?:timer|alarme))?|(?:quais (?:sao )?)?(?:os )?(?:meus )?(?:timers|alarmes|lembretes)(?: ativos)?",
        speak=True,
    ),
    _cmd(
        "timer",
        rf"(?:(?:cri\w+|coloc\w+|inici\w+|bot\w+|defin\w+|program\w+|marc\w+|liga\w*) )?(?:um )?(?:timer|temporizador|cronometro|alarme) (?:de |para |pra |por )?{_NUM} {_UNIT}",
        rf"(?:me )?(?:avis\w+|cham\w+) (?:em|daqui a|daqui) {_NUM} {_UNIT}",
        speak=True,
    ),
    _cmd(
        "alarm",
        r"(?:(?:cri\w+|coloc\w+|defin\w+|program\w+|bot\w+|liga\w*) )?(?:um )?(?:alarme|despertador) (?:para |pra |as |a )?(?P<clock>.+)",
        r"me (?:acord\w+|despert\w+) (?:as |a )?(?P<clock>.+)",
        speak=True,
    ),
    _cmd(
        "reminder",
        rf"(?:me )?lembr\w+(?:-me)? (?:de |que |do |da )?(?P<what>.+?) (?:em|daqui a|daqui) {_NUM} {_UNIT}",
        rf"(?:em|daqui a|daqui) {_NUM} {_UNIT} (?:me )?lembr\w+(?:-me)? (?:de |que |do |da )?(?P<what>.+)",
        r"(?:me )?lembr\w+(?:-me)? (?:de |que |do |da )?(?P<what>.+?) (?:as|a) (?P<clock>\d.*|[a-z]+(?: e [a-z]+)?(?: (?:da|de) (?:manha|tarde|noite))?)",
        speak=True,
    ),
    # ---------------------------- clipboard ------------------------------ #
    _cmd("clipboard_read", r"(?:o que (?:tem|ha|esta) )?(?:na )?area de transferencia|le\w* (?:a )?area de transferencia|o que (?:eu )?copiei", speak=True),
    _cmd("copy", r"copi\w+(?: (?:isso|isto|o texto|tudo|o selecionado))?|ctrl c"),
    _cmd("paste", r"col\w+(?: (?:aqui|isso|isto|o texto))?|ctrl v"),
    _cmd("cut", r"recort\w+(?: (?:isso|isto|o texto))?|ctrl x"),
    _cmd("undo", r"desfaz\w*|desfazer|ctrl z"),
    _cmd("redo", r"refaz\w*|refazer|ctrl y"),
    _cmd("select_all", r"selecion\w+ tudo|ctrl a"),
    _cmd("save", r"salv\w+(?: (?:o |isso|isto)?(?:arquivo|documento)?)?|ctrl s"),
    _cmd("new_document", rf"(?:cri\w+ |{_OPEN} )?(?:um )?novo (?:documento|arquivo)"),
    # -------------------------------- rede -------------------------------- #
    _cmd("wifi_on", r"(?:lig\w+|ativ\w+|habilit\w+|conect\w+) (?:o )?(?:wi ?-?fi|wireless|internet sem fio)|wi ?-?fi on"),
    _cmd(
        "wifi_off",
        r"(?:deslig\w+|desativ\w+|desabilit\w+|desconect\w+) (?:o )?(?:wi ?-?fi|wireless|internet sem fio)|wi ?-?fi off",
        confirm="Desligar o wi-fi? Eu perco o acesso à internet.",
    ),
    # ------------------------------ genéricos ---------------------------- #
    _cmd(
        "focus_window",
        r"(?:vai|volt\w+|mud\w+|troc\w+|alterna\w*) (?:para|pro|pra) (?:o |a )?(?P<app>.+)",
        max_words=3,
    ),
    _cmd("open_app", rf"{_OPEN} {_ART}(?P<app>.+)", say="Abrindo {app}.", max_words=4),
    _cmd("close_app", rf"{_CLOSE} {_ART}(?P<app>.+)", say="Fechando {app}.", max_words=4),
)


def _extract_params(match: re.Match[str], original: str, offset: int) -> dict[str, str]:
    """Recorta os grupos nomeados do texto original (com acentos)."""
    params: dict[str, str] = {}
    for key in match.groupdict():
        start, end = match.span(key)
        if start < 0:
            continue
        value = original[offset + start : offset + end].strip(" .,!?;:\"'")
        if value:
            params[key] = value
    return params


class Router:
    """Classifica frases em `Intent`s rápidos (regex) ou delega (Haiku/agente)."""

    def __init__(self, commands: tuple[FastCommand, ...] = FAST_COMMANDS) -> None:
        self.commands = commands
        self._client = None

    # ------------------------------------------------------------------ #
    # Regex
    # ------------------------------------------------------------------ #
    def match(self, text: str) -> Intent | None:
        """Tenta casar a frase com um comando rápido. Não usa rede."""
        if not text or not text.strip():
            return None
        folded = fold(text)
        begin, end = _trim_span(folded)
        core = re.sub(r"\s+", " ", folded[begin:end]).strip()
        # Espaços colapsados quebrariam o mapeamento de spans; guardamos o
        # original com a mesma transformação.
        # `str.lower()` quase sempre preserva o comprimento; se não preservar
        # (ex.: "İ"), caímos para a versão minúscula para manter o alinhamento.
        source = text if len(text) == len(folded) else text.lower()
        original_core = _collapse_like(source[begin:end], folded[begin:end])
        if not core:
            # A frase era só "obrigado"/"valeu": encerra a conversa.
            if re.search(r"\b(?:obrigad|valeu)", folded):
                command = self.get("cancel")
                return self._build(command, text, {}, "regex") if command else None
            return None

        for command in self.commands:
            for pattern in command.patterns:
                found = pattern.match(core)
                if not found:
                    continue
                params = _extract_params(found, original_core, 0)
                if command.max_words and any(
                    len(value.split()) > command.max_words for value in params.values()
                ):
                    continue
                return self._build(command, text, params, "regex")
        return None

    @staticmethod
    def _build(command: FastCommand, text: str, params: dict[str, str], source: str) -> Intent:
        title = settings.user_title
        fmt = {"title": title, **{key: value for key, value in params.items()}}
        fmt.setdefault("app", "")
        fmt.setdefault("q", "")
        return Intent(
            name=command.name,
            text=text,
            params=params,
            source=source,
            say_before=command.say_before.format(**fmt) if command.say_before else "",
            speak_result=command.speak_result,
            confirm=command.confirm.format(**fmt) if command.confirm else "",
            double_confirm=command.double_confirm,
        )

    def get(self, name: str) -> FastCommand | None:
        """Busca a definição de um comando pelo nome."""
        return next((command for command in self.commands if command.name == name), None)

    # ------------------------------------------------------------------ #
    # Haiku
    # ------------------------------------------------------------------ #
    def _anthropic(self):
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=settings.anthropic_api_key or None, max_retries=1)
        return self._client

    async def classify_llm(self, text: str, timeout: float = 6.0) -> dict[str, Any] | None:
        """Pede ao Haiku uma classificação JSON. Devolve None em qualquer falha."""
        if not settings.has_api_key:
            return None
        import anthropic

        try:
            response = await asyncio.wait_for(
                self._anthropic().messages.create(
                    model=settings.model_fast,
                    max_tokens=settings.max_tokens_fast,
                    system=router_system_prompt(),
                    messages=[{"role": "user", "content": text}],
                ),
                timeout=timeout,
            )
        except (TimeoutError, anthropic.APIConnectionError, anthropic.APIStatusError) as exc:
            log.warning("router.haiku_failed", error=str(exc))
            return None

        raw = "".join(block.text for block in response.content if block.type == "text").strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        brace = raw.find("{")
        if brace < 0:
            return None
        try:
            data = json.loads(raw[brace : raw.rfind("}") + 1])
        except json.JSONDecodeError:
            log.warning("router.haiku_bad_json", raw=raw[:200])
            return None
        return data if isinstance(data, dict) else None

    def from_classification(self, text: str, data: dict[str, Any]) -> Intent | None:
        """Converte o JSON do Haiku num `Intent` rápido, quando possível."""
        if data.get("needs_agent"):
            return None
        action = str(data.get("action", "")).strip().lower()
        target = str(data.get("target", "")).strip()
        lowered = fold(target).strip()

        def build(name: str, **params: str) -> Intent | None:
            command = self.get(name)
            if command is None:
                return None
            return self._build(command, text, {k: v for k, v in params.items() if v}, "haiku")

        simple = {
            ("window", "minimize_all"), ("window", "show_desktop"), ("window", "alt_tab"),
            ("window", "close_window"), ("window", "minimize_window"), ("window", "maximize_window"),
            ("window", "fullscreen"), ("system", "lock"), ("system", "sleep"), ("system", "restart"),
            ("system", "shutdown"), ("system", "task_manager"), ("system", "cancel_shutdown"),
            ("media", "play_pause"), ("media", "next_track"), ("media", "prev_track"),
            ("media", "stop_media"), ("volume", "mute"), ("volume", "unmute"), ("volume", "volume_up"),
            ("volume", "volume_down"), ("brightness", "brightness_up"), ("brightness", "brightness_down"),
            ("browser", "new_tab"), ("browser", "close_tab"), ("browser", "next_tab"),
            ("browser", "prev_tab"), ("browser", "reload"), ("browser", "gmail"), ("browser", "github"),
            ("clipboard", "copy"), ("clipboard", "paste"), ("clipboard", "clipboard_read"),
            ("info", "time"), ("info", "date"), ("info", "system_info"), ("screenshot", "screenshot"),
            ("screenshot", "snip"),
        }
        if (action, lowered) in simple:
            return build(lowered)

        match action:
            case "open_app" if target:
                return build("open_app", app=target)
            case "close_app" if target:
                return build("close_app", app=target)
            case "volume" if lowered.rstrip("%").isdigit():
                return build("volume_set", n=lowered.rstrip("%"))
            case "brightness" if lowered.rstrip("%").isdigit():
                return build("brightness_set", n=lowered.rstrip("%"))
            case "screenshot":
                return build("screenshot")
            case "info" if lowered.startswith("weather"):
                return build("weather", city=target.partition(":")[2].strip())
            case "timer" if lowered.isdigit():
                return build("timer", n=lowered, unit="segundos")
            case "browser" if lowered.startswith("search:"):
                return build("google", q=target.partition(":")[2].strip())
            case "browser" if lowered.startswith("youtube"):
                return build("youtube", q=target.partition(":")[2].strip())
            case "browser" if lowered.startswith("url:"):
                return build("open_url", url=target.partition(":")[2].strip())
            case "files" if lowered.startswith("folder:"):
                return build("open_folder", folder=target.partition(":")[2].strip())
        return None

    async def route(self, text: str) -> Intent | None:
        """
        Regex → Haiku. Devolve `None` quando a frase deve ir ao agente.
        """
        intent = self.match(text)
        if intent is not None:
            log.info("router.regex", intent=intent.describe())
            return intent
        data = await self.classify_llm(text)
        if data is None:
            return None
        intent = self.from_classification(text, data)
        log.info("router.haiku", data=data, intent=intent.describe() if intent else "agent")
        return intent


def _collapse_like(original: str, folded: str) -> str:
    """
    Aplica ao texto original o mesmo colapso de espaços feito no dobrado,
    mantendo os dois alinhados caractere a caractere.
    """
    out: list[str] = []
    previous_space = True  # também remove espaços iniciais
    for orig_char, fold_char in zip(original, folded, strict=False):
        if fold_char == " ":
            if previous_space:
                continue
            out.append(" ")
            previous_space = True
        else:
            out.append(orig_char)
            previous_space = False
    return "".join(out).rstrip()


__all__ = ["FAST_COMMANDS", "FastCommand", "Intent", "Router", "fold"]
