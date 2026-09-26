"""
Automação de navegador com Playwright (Chromium).

Usado pelo agente para tarefas que exigem ler a web: pesquisar, extrair
conteúdo de uma página, preencher formulários. As ações rápidas de aba
(Ctrl+T, Ctrl+W...) NÃO passam por aqui — elas são atalhos enviados ao
navegador que o usuário já tem aberto, em `executor.pc`.

O navegador sobe sob demanda (lazy) e é reaproveitado entre comandos.
"""
from __future__ import annotations

import asyncio
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import structlog

from config import DATA_DIR, settings

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class BrowserResult:
    """Resultado de uma ação de navegador."""

    ok: bool
    message: str
    url: str = ""
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def _clean(text: str, limit: int = 6000) -> str:
    """Compacta espaços em branco e corta o texto extraído."""
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = text.strip()
    return text[:limit] + ("\n... (truncado)" if len(text) > limit else "")


class BrowserController:
    """Wrapper assíncrono sobre o Playwright."""

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._lock = asyncio.Lock()
        self.available = False

    # ------------------------------------------------------------------ #
    async def start(self) -> bool:
        """Sobe o Chromium (idempotente). Retorna False se não der."""
        if self._page is not None:
            return True
        async with self._lock:
            if self._page is not None:
                return True
            try:
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=settings.browser_headless,
                    channel=None if settings.browser_channel == "chromium" else settings.browser_channel,
                    args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
                )
                self._context = await self._browser.new_context(
                    viewport=None if not settings.browser_headless else {"width": 1440, "height": 900},
                    locale="pt-BR",
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
                    ),
                    accept_downloads=True,
                )
                self._context.set_default_timeout(settings.browser_timeout_ms)
                self._page = await self._context.new_page()
                self.available = True
                log.info("browser.started", headless=settings.browser_headless)
                return True
            except Exception as exc:
                log.error("browser.start_failed", error=str(exc))
                self.available = False
                return False

    async def stop(self) -> None:
        """Fecha o navegador e libera os recursos."""
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:  # pragma: no cover
                    pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # pragma: no cover
                pass
        self._playwright = self._browser = self._context = self._page = None
        self.available = False
        log.info("browser.stopped")

    # ------------------------------------------------------------------ #
    async def navigate(self, url: str) -> BrowserResult:
        """Abre uma URL (aceita `exemplo.com` sem esquema)."""
        if not await self.start():
            return BrowserResult(False, "não consegui iniciar o navegador — rode `playwright install chromium`")
        target = url.strip()
        if not target:
            return BrowserResult(False, "URL vazia")
        if not target.startswith(("http://", "https://", "file://")):
            target = f"https://{target}"
        try:
            assert self._page is not None
            await self._page.goto(target, wait_until="domcontentloaded")
            title = await self._page.title()
            log.info("browser.navigate", url=target)
            return BrowserResult(True, f"abri {title or target}", self._page.url, data={"title": title})
        except Exception as exc:
            return BrowserResult(False, f"não consegui abrir {target}: {exc}")

    async def get_text(self, selector: str = "body", limit: int = 6000) -> BrowserResult:
        """Extrai o texto visível da página (ou de um seletor)."""
        if self._page is None:
            return BrowserResult(False, "nenhuma página aberta")
        try:
            element = await self._page.query_selector(selector)
            if element is None:
                return BrowserResult(False, f"seletor não encontrado: {selector}")
            text = _clean(await element.inner_text(), limit)
            return BrowserResult(True, "texto extraído", self._page.url, text)
        except Exception as exc:
            return BrowserResult(False, f"não consegui extrair o texto: {exc}")

    async def search(self, query: str, engine: str = "duckduckgo") -> BrowserResult:
        """Faz uma busca e devolve os primeiros resultados como texto."""
        if not await self.start():
            return BrowserResult(False, "navegador indisponível")
        encoded = urllib.parse.quote_plus(query)
        url = (
            f"https://duckduckgo.com/?q={encoded}"
            if engine == "duckduckgo"
            else f"https://www.google.com/search?q={encoded}&hl=pt-BR"
        )
        result = await self.navigate(url)
        if not result.ok:
            return result
        try:
            assert self._page is not None
            await self._page.wait_for_timeout(1200)
            body = await self._page.inner_text("body")
            return BrowserResult(True, f"resultados para {query}", self._page.url, _clean(body, 5000))
        except Exception as exc:
            return BrowserResult(False, f"não consegui ler os resultados: {exc}")

    async def search_and_extract(self, query: str, extract_prompt: str = "") -> BrowserResult:
        """
        Busca, abre o primeiro resultado orgânico e extrai o conteúdo.

        `extract_prompt` só é anexado ao resultado — quem interpreta é o
        modelo que chamou a ferramenta.
        """
        search_result = await self.search(query)
        if not search_result.ok:
            return search_result

        assert self._page is not None
        try:
            links = await self._page.query_selector_all("a[data-testid='result-title-a'], a.result__a, h3 a")
            for link in links[:3]:
                href = await link.get_attribute("href")
                if href and href.startswith("http") and "duckduckgo.com" not in href:
                    opened = await self.navigate(href)
                    if not opened.ok:
                        continue
                    await self._page.wait_for_timeout(800)
                    body = await self._page.inner_text("body")
                    return BrowserResult(
                        True,
                        f"conteúdo extraído de {self._page.url}",
                        self._page.url,
                        _clean(body, 8000),
                        {"extract_prompt": extract_prompt},
                    )
        except Exception as exc:
            log.warning("browser.extract_failed", error=str(exc))

        # Sem link utilizável: devolve a própria página de resultados.
        search_result.data["extract_prompt"] = extract_prompt
        return search_result

    async def action(self, action: str, selector: str = "", value: str = "") -> BrowserResult:
        """
        Executa uma ação na página: `click`, `fill`, `press`, `select`,
        `scroll`, `wait`, `screenshot`, `back`, `reload`.
        """
        if self._page is None:
            return BrowserResult(False, "nenhuma página aberta")
        page = self._page
        try:
            match action:
                case "click":
                    await page.click(selector)
                    return BrowserResult(True, f"cliquei em {selector}", page.url)
                case "fill":
                    await page.fill(selector, value)
                    return BrowserResult(True, f"preenchi {selector}", page.url)
                case "press":
                    await (page.press(selector, value) if selector else page.keyboard.press(value))
                    return BrowserResult(True, f"pressionei {value}", page.url)
                case "select":
                    await page.select_option(selector, value)
                    return BrowserResult(True, f"selecionei {value}", page.url)
                case "scroll":
                    await page.mouse.wheel(0, int(value or 600))
                    return BrowserResult(True, "rolei a página", page.url)
                case "wait":
                    await page.wait_for_timeout(int(value or 1000))
                    return BrowserResult(True, "aguardei", page.url)
                case "back":
                    await page.go_back()
                    return BrowserResult(True, "voltei", page.url)
                case "reload":
                    await page.reload()
                    return BrowserResult(True, "recarreguei", page.url)
                case "screenshot":
                    path = DATA_DIR / "screenshots" / (value or "page.png")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(path), full_page=False)
                    return BrowserResult(True, "captura da página salva", page.url, data={"path": str(path)})
                case _:
                    return BrowserResult(False, f"ação desconhecida: {action}")
        except Exception as exc:
            return BrowserResult(False, f"a ação {action} falhou: {exc}")

    @property
    def current_url(self) -> str:
        return self._page.url if self._page is not None else ""


__all__ = ["BrowserController", "BrowserResult"]
