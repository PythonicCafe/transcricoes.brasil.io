"""Mede o site estático num navegador de verdade (Chrome, via Playwright), cenário por cenário.

Sobe um servidor local com gzip para a pasta do site (como o GitHub Pages faz), abre cada cenário num contexto novo
(cache frio) e registra, a cada passo: tempo até a página ficar pronta, requisições, bytes transferidos (com gzip) e
descomprimidos, memória de JavaScript depois de coleta de lixo e número de nós do DOM.

Perfis: `computador` (sem limites) e `celular` (tela pequena, rede 4G simulada e CPU 4x mais lenta).

Requisições a outros domínios (YouTube) são bloqueadas e contadas à parte: o que se mede é o custo do próprio site.

Uso: python mede_site.py PASTA_DO_SITE --saida medicao.json [--repeticoes 3] [--perfis computador,celular]
Depois, `python compara.py antes.json depois.json` monta a tabela comparando duas medições.

Precisa do Playwright (`requirements-dev.txt`) e de um Chrome ou Chromium instalado; o `Dockerfile` traz os dois.
"""

import argparse
import functools
import gzip
import http.server
import json
import os
import shutil
import statistics
import threading
import time
import typing
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

# O navegador é o do sistema (no Dockerfile, o pacote `chromium` do Debian); a variável CHROME aponta outro executável.
CHROME = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome") or ""
PERFIS = {
    "computador": {"tela": {"width": 1366, "height": 768}, "rede": None, "cpu": 1},
    # 4G simulado: 9 Mbit/s de descida, 1,5 Mbit/s de subida, 170 ms de latência; CPU 4x mais lenta.
    "celular": {"tela": {"width": 390, "height": 844}, "rede": (9_000_000 / 8, 1_500_000 / 8, 170), "cpu": 4},
}
COMPRIMIVEIS = (".html", ".js", ".css", ".json", ".vtt", ".svg", ".txt")
ESPERA_MAXIMA = 120_000


class Servidor(http.server.SimpleHTTPRequestHandler):
    """Arquivos estáticos com gzip para texto, sem cache, HTTP/1.1 com conexão persistente."""

    protocol_version = "HTTP/1.1"
    comprimidos: typing.ClassVar[dict[str, bytes]] = {}

    def do_GET(self) -> None:
        caminho = Path(self.translate_path(self.path.split("?")[0].split("#")[0]))
        if caminho.is_dir():
            caminho = caminho / "index.html"
        if not caminho.is_file():
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        corpo = caminho.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", self.guess_type(str(caminho)))
        if caminho.suffix in COMPRIMIVEIS and "gzip" in self.headers.get("Accept-Encoding", ""):
            chave = str(caminho)
            if chave not in self.comprimidos:
                self.comprimidos[chave] = gzip.compress(corpo, compresslevel=6)
            corpo = self.comprimidos[chave]
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "max-age=600")
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, *argumentos) -> None:
        pass


class Medidor:
    """Acumula as requisições de uma página e tira uma fotografia das métricas a cada passo."""

    def __init__(self, page: Page, origem: str, perfil: dict) -> None:
        self.page = page
        self.origem = origem
        self.cdp = page.context.new_cdp_session(page)
        self.cdp.send("Network.enable")
        self.cdp.send("Performance.enable")
        self.cdp.send("HeapProfiler.enable")
        if perfil["rede"]:
            descida, subida, latencia = perfil["rede"]
            self.cdp.send(
                "Network.emulateNetworkConditions",
                {"offline": False, "downloadThroughput": descida, "uploadThroughput": subida, "latency": latencia},
            )
        if perfil["cpu"] > 1:
            self.cdp.send("Emulation.setCPUThrottlingRate", {"rate": perfil["cpu"]})
        self.enderecos: dict[str, str] = {}
        self.terminadas: list[tuple[str, int]] = []
        self.descomprimido = 0
        self.bloqueadas = 0
        self.passos: list[dict] = []
        self.cdp.on(
            "Network.requestWillBeSent",
            lambda evento: self.enderecos.__setitem__(evento["requestId"], evento["request"]["url"]),
        )
        self.cdp.on("Network.loadingFinished", self._terminou)
        self.cdp.on("Network.dataReceived", self._recebeu)
        page.route("**/*", self._roteia)

    def _roteia(self, rota) -> None:
        if rota.request.url.startswith(self.origem):
            rota.continue_()
        else:
            self.bloqueadas += 1
            rota.abort()

    def _terminou(self, evento: dict) -> None:
        endereco = self.enderecos.get(evento["requestId"], "")
        if endereco.startswith(self.origem):
            self.terminadas.append((endereco[len(self.origem) :], int(evento["encodedDataLength"])))

    def _recebeu(self, evento: dict) -> None:
        if self.enderecos.get(evento["requestId"], "").startswith(self.origem):
            self.descomprimido += int(evento["dataLength"])

    def passo(self, nome: str, acao, pronto: str) -> None:
        """Executa `acao`, espera a expressão JavaScript `pronto` ficar verdadeira e registra o que mudou."""
        antes_requisicoes = len(self.terminadas)
        antes_transferido = sum(tamanho for _, tamanho in self.terminadas)
        antes_descomprimido = self.descomprimido
        inicio = time.perf_counter()
        acao()
        self.page.wait_for_function(pronto, timeout=ESPERA_MAXIMA)
        duracao = time.perf_counter() - inicio
        self.page.wait_for_load_state("networkidle", timeout=ESPERA_MAXIMA)
        self.cdp.send("HeapProfiler.collectGarbage")
        metricas = {item["name"]: item["value"] for item in self.cdp.send("Performance.getMetrics")["metrics"]}
        novas = self.terminadas[antes_requisicoes:]
        self.passos.append(
            {
                "passo": nome,
                "segundos": round(duracao, 3),
                "requisicoes": len(novas),
                "transferido": sum(tamanho for _, tamanho in novas),
                "descomprimido": self.descomprimido - antes_descomprimido,
                "memoria_js": int(metricas["JSHeapUsedSize"]),
                "nos_dom": int(metricas["Nodes"]),
                "arquivos": [endereco for endereco, _ in novas],
                "transferido_total": antes_transferido + sum(tamanho for _, tamanho in novas),
                "estado": self.page.evaluate(ESTADO_DA_PAGINA),
            }
        )


# O que está na tela, para conferir que antes e depois mostram a mesma coisa.
ESTADO_DA_PAGINA = """() => ({
  hash: location.hash,
  cartoes: document.querySelectorAll('.result-card').length,
  contagem: (document.querySelector('.result-count') || {}).textContent || '',
  trechos: document.querySelectorAll('.passage-list__item').length,
  falas: document.querySelectorAll('.transcript-list__line').length,
  titulo: (document.querySelector('h1') || {}).textContent || '',
  primeiro: (document.querySelector('.result-card__title') || {}).textContent || '',
  resumo_na_sessao: ((document.querySelector('.machine-box p') || {}).textContent || '').length,
})"""

HOME_PRONTA = "() => document.querySelectorAll('.result-card').length > 0"
SESSAO_PRONTA = "() => document.querySelectorAll('.transcript-list__line').length > 0"
BUSCA_PRONTA = "() => !!document.querySelector('.result-count strong, .empty-state')"
PAGINA_2_PRONTA = "() => !!document.querySelector('.page-nav__button[aria-current=page][data-pagina=\\'2\\']')"
SOBRE_PRONTO = "() => !!document.querySelector('.prose-block p')"


def abre(medidor: Medidor, origem: str, hash_da_pagina: str = ""):
    return lambda: medidor.page.goto(f"{origem}/{hash_da_pagina}", wait_until="commit")


def busca(medidor: Medidor, consulta: str):
    def acao() -> None:
        medidor.page.fill("#consulta", consulta)
        medidor.page.press("#consulta", "Enter")
        # O estado "carregando" não tem <strong>; a contagem final ou o aviso de vazio têm.
        medidor.page.wait_for_function(
            "(consulta) => { const forte = document.querySelector('.result-count strong');"
            " return (forte && forte.textContent === consulta) || !!document.querySelector('.empty-state'); }",
            arg=consulta,
            timeout=ESPERA_MAXIMA,
        )

    return acao


def filtra(medidor: Medidor, casa: str, tema: str, de: str):
    def acao() -> None:
        medidor.page.evaluate(
            """([casa, tema, de]) => {
                const caixa = [...document.querySelectorAll('#casas input')].find((item) => item.value === casa);
                caixa.checked = true;
                document.getElementById('tema').value = tema;
                document.getElementById('de').value = de;
                document.getElementById('de').dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            [casa, tema, de],
        )

    return acao


def clica(medidor: Medidor, seletor: str):
    return lambda: medidor.page.evaluate("(seletor) => document.querySelector(seletor).click()", seletor)


def cenarios(origem: str, sessao: str) -> dict:
    """Cada cenário é uma lista de passos (nome, ação, condição de pronto); `sessao` é o id usado nos links diretos."""
    return {
        "1. Só a home": lambda m: [("home", abre(m, origem), HOME_PRONTA)],
        "2. Home > abre uma sessão": lambda m: [
            ("home", abre(m, origem), HOME_PRONTA),
            ("abre a 1ª sessão", clica(m, ".result-card__title a"), SESSAO_PRONTA),
        ],
        "3. Home > filtros > abre uma sessão": lambda m: [
            ("home", abre(m, origem), HOME_PRONTA),
            ("filtra casa, tema e data", filtra(m, "Câmara", "Saúde", "2025-01-01"), HOME_PRONTA),
            ("abre a 1ª sessão", clica(m, ".result-card__title a"), SESSAO_PRONTA),
        ],
        "4. Home > busca": lambda m: [
            ("home", abre(m, origem), HOME_PRONTA),
            ("busca orçamento", busca(m, "orçamento"), BUSCA_PRONTA),
        ],
        "5. Home > busca > abre um trecho": lambda m: [
            ("home", abre(m, origem), HOME_PRONTA),
            ("busca orçamento", busca(m, "orçamento"), BUSCA_PRONTA),
            ("abre o 1º trecho", clica(m, ".passage-list .time-link"), SESSAO_PRONTA),
        ],
        "6. Home > três buscas": lambda m: [
            ("home", abre(m, origem), HOME_PRONTA),
            ("busca orçamento", busca(m, "orçamento"), BUSCA_PRONTA),
            ("busca saúde", busca(m, "saúde"), BUSCA_PRONTA),
            ("busca emenda parlamentar", busca(m, "emenda parlamentar"), BUSCA_PRONTA),
        ],
        "7. Link direto para uma sessão": lambda m: [("sessão", abre(m, origem, f"#/v/{sessao}"), SESSAO_PRONTA)],
        "8. Link direto para um trecho": lambda m: [
            ("sessão no minuto 10", abre(m, origem, f"#/v/{sessao}/600"), SESSAO_PRONTA)
        ],
        "9. Home > página Sobre": lambda m: [
            ("home", abre(m, origem), HOME_PRONTA),
            ("sobre", clica(m, "a[href='#/sobre']"), SOBRE_PRONTO),
        ],
        "10. Home > página 2 > abre uma sessão": lambda m: [
            ("home", abre(m, origem), HOME_PRONTA),
            ("vai para a página 2", clica(m, "[data-pagina='2']"), PAGINA_2_PRONTA),
            ("abre a 1ª sessão", clica(m, ".result-card__title a"), SESSAO_PRONTA),
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("site", type=Path, help="pasta servida (o docs/ gerado)")
    parser.add_argument("--saida", type=Path, required=True)
    parser.add_argument("--repeticoes", type=int, default=3)
    parser.add_argument("--perfis", default="computador,celular")
    parser.add_argument("--sessao", default="", help="id da sessão dos links diretos (padrão: a de tamanho mediano)")
    parser.add_argument("--porta", type=int, default=8765)
    args = parser.parse_args()
    if not CHROME:
        parser.error("nenhum Chrome ou Chromium encontrado; instale um ou defina a variável de ambiente CHROME")
    if not args.sessao:
        tamanhos = sorted(
            (caminho.stat().st_size, caminho.stem) for caminho in (args.site / "dados" / "itens").glob("*.json")
        )
        if not tamanhos:
            parser.error(f"{args.site}/dados/itens/ está vazia; gere o site antes ou informe --sessao")
        args.sessao = tamanhos[len(tamanhos) // 2][1]

    servidor = http.server.ThreadingHTTPServer(
        ("127.0.0.1", args.porta), functools.partial(Servidor, directory=str(args.site))
    )
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    origem = f"http://127.0.0.1:{args.porta}"
    resultado = {"site": str(args.site), "repeticoes": args.repeticoes, "sessao": args.sessao, "perfis": {}}
    try:
        with sync_playwright() as playwright:
            navegador = playwright.chromium.launch(
                executable_path=CHROME, args=["--no-sandbox", "--js-flags=--expose-gc"]
            )
            for nome_do_perfil in [nome.strip() for nome in args.perfis.split(",") if nome.strip()]:
                perfil = PERFIS[nome_do_perfil]
                resultado["perfis"][nome_do_perfil] = {}
                for nome_do_cenario in cenarios(origem, args.sessao):
                    rodadas = []
                    for _ in range(args.repeticoes):
                        contexto = navegador.new_context(viewport=perfil["tela"], locale="pt-BR")
                        page = contexto.new_page()
                        medidor = Medidor(page, origem, perfil)
                        for nome, acao, pronto in cenarios(origem, args.sessao)[nome_do_cenario](medidor):
                            medidor.passo(nome, acao, pronto)
                        rodadas.append({"passos": medidor.passos, "bloqueadas": medidor.bloqueadas})
                        contexto.close()
                    passos = []
                    for posicao, passo in enumerate(rodadas[0]["passos"]):
                        mediano = dict(passo)
                        for campo in ("segundos", "memoria_js"):
                            mediano[campo] = statistics.median(rodada["passos"][posicao][campo] for rodada in rodadas)
                        passos.append(mediano)
                    resultado["perfis"][nome_do_perfil][nome_do_cenario] = {
                        "passos": passos,
                        "bloqueadas": rodadas[0]["bloqueadas"],
                    }
                    resumo = " | ".join(
                        f"{passo['passo']}: {passo['segundos']:.2f} s, {passo['requisicoes']} req, {passo['transferido'] / 1024:.0f} KB"
                        for passo in passos
                    )
                    print(f"[{nome_do_perfil}] {nome_do_cenario}: {resumo}", flush=True)
            navegador.close()
    finally:
        servidor.shutdown()
    args.saida.write_text(json.dumps(resultado, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"gravado em {args.saida}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("interrompido") from None
