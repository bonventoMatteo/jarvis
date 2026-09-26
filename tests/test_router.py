"""Testes do roteador regex (sem rede)."""
from __future__ import annotations

import pytest

from llm.router import FAST_COMMANDS, Router, fold

router = Router()


@pytest.mark.parametrize(
    ("phrase", "intent", "params"),
    [
        # apps
        ("Abrir o Chrome", "open_app", {"app": "Chrome"}),
        ("Jarvis, abre o Spotify por favor.", "open_app", {"app": "Spotify"}),
        ("inicia o visual studio code", "open_app", {"app": "visual studio code"}),
        ("fecha o Discord", "close_app", {"app": "Discord"}),
        ("encerrar o word", "close_app", {"app": "word"}),
        # janelas
        ("minimizar tudo", "minimize_all", {}),
        ("mostrar a área de trabalho", "show_desktop", {}),
        ("alt tab", "alt_tab", {}),
        ("próxima janela", "alt_tab", {}),
        ("fechar janela", "close_window", {}),
        ("maximizar", "maximize_window", {}),
        ("minimiza essa janela", "minimize_window", {}),
        ("tela cheia", "fullscreen", {}),
        ("vai para o chrome", "focus_window", {"app": "chrome"}),
        # sistema
        ("bloquear o computador", "lock", {}),
        ("bloqueia", "lock", {}),
        ("coloca o pc pra dormir", "sleep", {}),
        ("reiniciar o computador", "restart", {}),
        ("desligar o PC", "shutdown", {}),
        ("cancela o desligamento", "cancel_shutdown", {}),
        ("abrir o gerenciador de tarefas", "task_manager", {}),
        ("abre o prompt de comando como administrador", "admin_terminal", {"shell": "prompt de comando"}),
        ("powershell como admin", "admin_terminal", {"shell": "powershell"}),
        # mídia
        ("pausa a música", "play_pause", {}),
        ("play", "play_pause", {}),
        ("próxima música", "next_track", {}),
        ("pula essa faixa", "next_track", {}),
        ("música anterior", "prev_track", {}),
        ("para a música", "stop_media", {}),
        # volume
        ("volume 50", "volume_set", {"n": "50"}),
        ("coloca o volume em 30%", "volume_set", {"n": "30"}),
        ("volume no máximo", "volume_set", {"level": "máximo"}),
        ("deixa o volume em cinquenta", "volume_set", {"n": "cinquenta"}),
        ("aumenta o volume", "volume_up", {}),
        ("abaixa o som", "volume_down", {}),
        ("mudo", "mute", {}),
        ("tira do mudo", "unmute", {}),
        ("qual é o volume", "volume_get", {}),
        # tela / brilho
        ("tira um print", "screenshot", {}),
        ("print da tela", "screenshot", {}),
        ("print de uma região", "snip", {}),
        ("aumenta o brilho", "brightness_up", {}),
        ("diminui o brilho", "brightness_down", {}),
        ("brilho em 70 por cento", "brightness_set", {"n": "70"}),
        # navegador
        ("nova aba", "new_tab", {}),
        ("fecha a aba", "close_tab", {}),
        ("próxima aba", "next_tab", {}),
        ("aba anterior", "prev_tab", {}),
        ("reabre a aba", "reopen_tab", {}),
        ("recarregar a página", "reload", {}),
        ("abrir o histórico", "history", {}),
        ("downloads do navegador", "browser_downloads", {}),
        ("pesquisa no Google receita de bolo de cenoura", "google", {"q": "receita de bolo de cenoura"}),
        ("busca clima em Lisboa no google", "google", {"q": "clima em Lisboa"}),
        ("abrir o YouTube", "youtube", {}),
        ("toca Daft Punk no YouTube", "youtube", {"q": "Daft Punk"}),
        ("abrir o gmail", "gmail", {}),
        ("abrir o GitHub", "github", {}),
        ("abre o site wikipedia.org", "open_url", {"url": "wikipedia.org"}),
        # arquivos
        ("abrir a pasta downloads", "open_folder", {"folder": "downloads"}),
        ("abre os documentos", "open_folder", {"folder": "documentos"}),
        ("abrir a pasta de Músicas", "open_folder", {"folder": "Músicas"}),
        ("criar pasta Projetos aqui", "create_folder", {"name": "Projetos", "here": "aqui"}),
        ("cria uma nova pasta", "create_folder", {}),
        ("cria uma pasta chamada Relatórios na área de trabalho", "create_folder", {"name": "Relatórios", "parent": "área de trabalho"}),
        ("buscar arquivo contrato.pdf", "search_file", {"q": "contrato.pdf"}),
        ("apagar o arquivo rascunho.txt", "delete_file", {"q": "rascunho.txt"}),
        ("esvaziar a lixeira", "empty_trash", {}),
        ("abre o arquivo orçamento", "open_file", {"q": "orçamento"}),
        # info
        ("que horas são?", "time", {}),
        ("que dia é hoje", "date", {}),
        ("clima em São Paulo", "weather", {"city": "São Paulo"}),
        ("como está o tempo", "weather", {}),
        ("status do sistema", "system_info", {}),
        # timers / agenda
        ("timer de 5 minutos", "timer", {"n": "5", "unit": "minutos"}),
        ("coloca um timer de dez segundos", "timer", {"n": "dez", "unit": "segundos"}),
        ("cancela o timer", "cancel_timer", {}),
        ("alarme para 7:30", "alarm", {"clock": "7:30"}),
        ("me acorda às 6 e meia", "alarm", {"clock": "6 e meia"}),
        ("me lembra de ligar para o João em 20 minutos", "reminder", {"what": "ligar para o João", "n": "20", "unit": "minutos"}),
        ("lembre-me de tomar remédio às 22h", "reminder", {"what": "tomar remédio", "clock": "22h"}),
        ("quanto falta", "list_schedule", {}),
        # clipboard / edição
        ("copiar", "copy", {}),
        ("cola aqui", "paste", {}),
        ("o que tem na área de transferência", "clipboard_read", {}),
        ("desfazer", "undo", {}),
        ("salvar", "save", {}),
        ("novo documento", "new_document", {}),
        ("selecionar tudo", "select_all", {}),
        # rede
        ("liga o wifi", "wifi_on", {}),
        ("desligar o wi-fi", "wifi_off", {}),
        # meta
        ("cancela", "cancel", {}),
        ("repete", "repeat", {}),
        ("desligar o jarvis", "quit", {}),
    ],
)
def test_fast_commands(phrase: str, intent: str, params: dict[str, str]) -> None:
    result = router.match(phrase)
    assert result is not None, phrase
    assert result.name == intent, (phrase, result.describe())
    for key, value in params.items():
        assert result.params.get(key) == value, (phrase, result.params)


@pytest.mark.parametrize(
    "phrase",
    [
        "escreve um email para o meu chefe dizendo que vou me atrasar",
        "qual a capital da Austrália e quantos habitantes tem",
        "abre o vscode no projeto jarvis e roda os testes do backend",
        "resuma a página que está aberta no navegador",
        "",
        "   ",
    ],
)
def test_complex_goes_to_agent(phrase: str) -> None:
    assert router.match(phrase) is None


def test_fold_preserves_length() -> None:
    text = "Ação! Pé-de-moleque, ÇÃO? Olá, São João."
    folded = fold(text)
    assert len(folded) == len(text)
    assert "acao" in folded and "sao joao" in folded


def test_at_least_fifty_commands() -> None:
    assert len(FAST_COMMANDS) >= 50
    assert len({command.name for command in FAST_COMMANDS}) == len(FAST_COMMANDS)


def test_confirmation_flags() -> None:
    shutdown = router.match("desligar o computador")
    assert shutdown is not None and shutdown.confirm and shutdown.double_confirm
    delete = router.match("apagar o arquivo notas.txt")
    assert delete is not None and "notas.txt" in delete.confirm
    assert router.match("abrir o chrome").confirm == ""


def test_say_before_uses_original_text() -> None:
    intent = router.match("abre o Spotify")
    assert intent is not None
    assert intent.say_before == "Abrindo Spotify."


def test_haiku_classification_mapping() -> None:
    intent = router.from_classification("põe no volume 40", {"action": "volume", "target": "40", "needs_agent": False})
    assert intent is not None and intent.name == "volume_set" and intent.params["n"] == "40"
    intent = router.from_classification("x", {"action": "browser", "target": "search:gatos", "needs_agent": False})
    assert intent is not None and intent.name == "google" and intent.params["q"] == "gatos"
    intent = router.from_classification("x", {"action": "info", "target": "weather:Recife", "needs_agent": False})
    assert intent is not None and intent.name == "weather" and intent.params["city"] == "Recife"
    assert router.from_classification("x", {"action": "chat", "target": "", "needs_agent": True}) is None
    assert router.from_classification("x", {"action": "files", "target": "algo", "needs_agent": False}) is None
