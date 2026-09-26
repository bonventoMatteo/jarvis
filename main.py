"""
JARVIS — ponto de entrada.

Uso:
    python main.py                 # modo completo: palmas + wake word + Ctrl+Alt+J + painel
    python main.py --text          # digita comandos em vez de falar (sem microfone)
    python main.py --no-dashboard  # logs coloridos no console em vez do painel
    python main.py --list-devices  # lista microfones/saídas de áudio
    python main.py --no-clap --no-wake --skip-calibration --mute --debug
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys

from rich.console import Console

from config import BASE_DIR, IS_LINUX, IS_WINDOWS, settings
from core.logging_setup import configure_logging


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description="JARVIS — assistente de voz cinematográfico para Windows.",
    )
    parser.add_argument("--text", action="store_true", help="modo texto: digite os comandos (sem microfone)")
    parser.add_argument("--no-dashboard", action="store_true", help="desativa o painel rich (mostra logs)")
    parser.add_argument("--no-clap", action="store_true", help="desativa a ativação por palmas")
    parser.add_argument("--no-wake", action="store_true", help="desativa o wake word")
    parser.add_argument("--no-hotkey", action="store_true", help="desativa o atalho global")
    parser.add_argument("--skip-calibration", action="store_true", help="pula os 5 s de calibração do ruído")
    parser.add_argument("--mute", action="store_true", help="não fala (só mostra as respostas)")
    parser.add_argument("--list-devices", action="store_true", help="lista os dispositivos de áudio e sai")
    parser.add_argument("--debug", action="store_true", help="log em nível DEBUG")
    parser.add_argument(
        "--activate", action="store_true", help="ativa um JARVIS já em execução (use como atalho do sistema)"
    )
    parser.add_argument("--send", metavar="COMANDO", help="envia um comando de texto a um JARVIS em execução")
    return parser.parse_args(argv)


def _list_devices(console: Console) -> int:
    try:
        from audio.mic import list_devices

        for line in list_devices():
            console.print(line)
        console.print(
            "\nDefina [cyan]INPUT_DEVICE[/] / [cyan]OUTPUT_DEVICE[/] no .env com o índice desejado."
        )
        return 0
    except Exception as exc:
        console.print(f"[red]Não consegui listar os dispositivos:[/] {exc}")
        return 1


def _preflight(console: Console, text_mode: bool) -> None:
    """Avisos amigáveis antes do boot (não impedem a execução)."""
    if not (BASE_DIR / ".env").exists():
        console.print("[yellow]Aviso:[/] arquivo .env não encontrado — usando padrões. Copie .env.example para .env.")
    if not settings.has_api_key:
        console.print(
            "[yellow]Aviso:[/] ANTHROPIC_API_KEY ausente. Comandos rápidos funcionam; tarefas complexas não."
        )
    if IS_LINUX:
        from executor import linux

        missing = [tool for tool in ("wmctrl", "xdotool", "pactl", "playerctl", "brightnessctl", "nmcli")
                   if not linux.have(tool)]
        if missing:
            console.print(f"[yellow]Aviso:[/] ferramentas ausentes: {', '.join(missing)} (rode ./install.sh).")
        if linux.session_type() == "wayland":
            console.print("[yellow]Aviso:[/] sessão Wayland: controle de janelas limitado; veja o README.")
    elif not IS_WINDOWS:
        console.print("[yellow]Aviso:[/] sistema não suportado oficialmente; várias ações vão falhar.")
    if not text_mode:
        piper_model = BASE_DIR / "models" / "piper" / f"{settings.piper_voice}.onnx"
        if not piper_model.exists():
            console.print(
                f"[yellow]Aviso:[/] voz piper ausente ({piper_model.name}). Rode "
                "[cyan]python -m scripts.download_models[/] — até lá uso a voz do Windows."
            )


async def _console_printer(orchestrator) -> None:
    """No modo sem painel, imprime falas e ações de forma legível."""
    from core.events import EventType

    console = Console()
    queue = orchestrator.bus.subscribe()
    try:
        async for event in orchestrator.bus.stream(queue):
            match event.type:
                case EventType.SPEAKING_TEXT:
                    console.print(f"[bold bright_cyan]{settings.assistant_name}:[/] {event.get('text', '')}")
                case EventType.TOOL_CALL:
                    console.print(f"[bright_black]  → {event.get('name')} {event.get('args', {})}[/]")
                case EventType.TOOL_RESULT:
                    mark = "[green]✓[/]" if event.get("ok") else "[red]✗[/]"
                    console.print(f"[bright_black]  {mark} {event.get('preview', '')[:160]}[/]")
                case EventType.NOTICE:
                    console.print(f"[bright_black]· {event.get('text', '')}[/]")
                case EventType.LATENCY:
                    console.print(f"[bright_black]  ({event.get('total_ms')} ms)[/]")
    finally:
        orchestrator.bus.unsubscribe(queue)


async def _amain(args: argparse.Namespace, console: Console) -> int:
    from core.orchestrator import Orchestrator
    from ui.dashboard import Dashboard, splash

    use_dashboard = settings.dashboard_enabled and not args.no_dashboard and not args.text
    orchestrator = Orchestrator(
        text_mode=args.text,
        enable_clap=False if args.no_clap else None,
        enable_wake=False if args.no_wake else None,
        enable_hotkey=False if args.no_hotkey else None,
        skip_calibration=args.skip_calibration,
        mute_voice=args.mute,
    )

    loop = asyncio.get_running_loop()
    if not IS_WINDOWS:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, orchestrator.stop)

    ui_task: asyncio.Task[None] | None = None
    if use_dashboard:
        ui_task = asyncio.create_task(Dashboard(orchestrator, console).run(), name="dashboard")
    else:
        splash(console)
        if args.text:
            console.print("[bright_black]Modo texto — digite um comando (ou 'sair').[/]\n")
        ui_task = asyncio.create_task(_console_printer(orchestrator), name="printer")

    try:
        await orchestrator.run()
    except RuntimeError as exc:
        # Falha de hardware no boot (microfone) — mensagem clara, sem traceback.
        if ui_task is not None:
            ui_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ui_task
        console.print(f"[red]Erro:[/] {exc}")
        console.print("Dica: [cyan]python main.py --list-devices[/] ou [cyan]python main.py --text[/].")
        return 2
    finally:
        if ui_task is not None and not ui_task.done():
            ui_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ui_task
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entrada síncrona (usada por `python main.py`)."""
    args = _parse_args(argv)
    console = Console()

    if args.list_devices:
        return _list_devices(console)
    if args.activate or args.send:
        from core.ipc import send

        try:
            reply = send("ACTIVATE" if args.activate else f"TEXT {args.send}")
        except OSError:
            console.print("[red]O JARVIS não está em execução (ou IPC_PORT difere).[/]")
            return 1
        return 0 if reply.startswith("OK") else 1

    use_dashboard = settings.dashboard_enabled and not args.no_dashboard and not args.text
    configure_logging(console=not use_dashboard and not args.text, level="DEBUG" if args.debug else None)
    _preflight(console, args.text)

    try:
        return asyncio.run(_amain(args, console))
    except KeyboardInterrupt:
        console.print("\n[bright_black]Encerrado.[/]")
        return 0


if __name__ == "__main__":
    sys.exit(main())
