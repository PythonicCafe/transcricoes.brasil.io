"""Monta o site estático (pasta `docs/`, servida pelo GitHub Pages) a partir da saída do `pipeline.py`.

Entram só os vídeos com o pipeline completo: linha em `dados/videos.jsonl`, `dados/transcricao/<id>.vtt` e `.txt` e
`dados/resumo/<id>.json`. Arquivos temporários (`*.tmp.*`) são ignorados. Use `--excluir` para tirar vídeos do site.

Saída:
- `docs/index.html`, `docs/js/`, `docs/vendor/` e o logo: cópia de `site/`;
- `docs/css/app.css`: os arquivos de `site/css/` juntos num só, sem comentários;
- `docs/legendas/<id>.vtt`: legenda de cada sessão em WebVTT, para download;
- `docs/miniaturas/<id>.webp`: miniatura de cada vídeo, baixada do YouTube uma vez, convertida para WebP e servida
  pelo próprio site;
- `docs/dados/itens.json`: lista enxuta para a página inicial e os filtros (linhas na ordem de `campos`; casa, UF,
  tipo e temas como posição nas listas de opções);
- `docs/dados/busca.json`: o que a busca varre, `{"falas": {id: [[segundos, texto], ...]}, "palavras_chave": {id: [...]}}`;
- `docs/dados/itens/<id>.json`: uma sessão completa (metadados, resumo, participantes, decisões e falas);
- `docs/CNAME` e `docs/.nojekyll`.

A busca roda no navegador, sobre todas as transcrições. Casa legislativa e UF vêm do CSV de vídeos (colunas `url`,
`uf` e `orgao`), se ele for informado; sem ele, a casa é o nome do canal.
"""

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from tqdm import tqdm

from pipeline import PASTA_DADOS, id_do_video, le_jsonl

REPOSITORIO = "https://github.com/PythonicCafe/transcricoes.brasil.io"
DOMINIO = "transcricoes.brasil.io"


def segundos_da_marca(marca: str) -> int:
    horas, minutos, segundos = (int(parte) for parte in marca.split(":"))
    return horas * 3600 + minutos * 60 + segundos


def le_transcricao(caminho: Path) -> list[list]:
    """Linhas `[HH:MM:SS] texto` viram `[segundos, texto]`; linhas fora do formato são ignoradas."""
    linhas = []
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha.startswith("[") or "]" not in linha:
            continue
        marca, texto = linha[1:].split("]", 1)
        try:
            segundos = segundos_da_marca(marca)
        except ValueError:
            continue
        if texto.strip():
            linhas.append([segundos, texto.strip()])
    return linhas


QUALIDADE_WEBP = 70


def baixa_miniatura(video_id: str, pasta: Path) -> bool:
    """Garante `miniaturas/<id>.webp`; devolve False se o download falhar (o site usa então a miniatura do YouTube).

    O YouTube entrega JPEG; o ffmpeg converte para WebP, que sai cerca de um quarto menor nesta qualidade. Um
    `<id>.jpg` de geração anterior é convertido sem novo download. O JPEG é apagado depois da conversão.
    """
    destino = pasta / f"{video_id}.webp"
    if destino.exists():
        return True
    original = pasta / f"{video_id}.jpg"
    if not original.exists():
        try:
            with urllib.request.urlopen(f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg", timeout=20) as resposta:
                conteudo = resposta.read()
        except (urllib.error.URLError, TimeoutError) as erro:
            print(f"AVISO: miniatura de {video_id} não baixada ({erro}); o site vai usar a do YouTube")
            return False
        original.write_bytes(conteudo)
    temporario = pasta / f"{video_id}.tmp.webp"
    comando = ["ffmpeg", "-loglevel", "error", "-y", "-i", str(original), "-c:v", "libwebp"]
    try:
        subprocess.run(
            [*comando, "-quality", str(QUALIDADE_WEBP), str(temporario)], capture_output=True, check=True, timeout=60
        )
    except FileNotFoundError:
        sys.exit("ERRO: o ffmpeg não está instalado; ele converte as miniaturas para WebP")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as erro:
        temporario.unlink(missing_ok=True)
        sys.exit(f"ERRO: o ffmpeg não converteu a miniatura de {video_id}: {erro}")
    temporario.rename(destino)
    original.unlink()
    return True


# Campos de cada linha de `itens.json`, na ordem; os de CATEGORICOS e os temas vão como posição na lista de opções.
CAMPOS_DA_LISTA = ["id", "titulo", "casa", "uf", "data", "duracao", "tipo", "temas", "resumo"]
CATEGORICOS = {"casa": "casas", "uf": "ufs", "tipo": "tipos"}


def lista_enxuta(sessoes: list[dict], config: dict) -> dict:
    """Monta o `itens.json`: só o que a página inicial e os filtros usam, sem repetir as strings categóricas."""
    opcoes = {nome: sorted({sessao[campo] for sessao in sessoes}) for campo, nome in CATEGORICOS.items()}
    opcoes["temas"] = sorted({tema for sessao in sessoes for tema in sessao["temas"]})
    posicoes = {nome: {valor: posicao for posicao, valor in enumerate(valores)} for nome, valores in opcoes.items()}
    itens = []
    for sessao in sessoes:
        linha = []
        for campo in CAMPOS_DA_LISTA:
            if campo in CATEGORICOS:
                linha.append(posicoes[CATEGORICOS[campo]][sessao[campo]])
            elif campo == "temas":
                linha.append([posicoes["temas"][tema] for tema in sessao["temas"]])
            else:
                linha.append(sessao[campo])
        itens.append(linha)
    return {"config": config, **opcoes, "campos": CAMPOS_DA_LISTA, "itens": itens}


RE_IMPORT = re.compile(r'@import url\("([^"]+)"\);')


def enxuga_css(css: str) -> str:
    """Tira comentários e espaços que não fazem diferença; não mexe em seletores nem em valores."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    css = re.sub(r"\s+", " ", css)
    return re.sub(r" ?([{};]) ?", r"\1", css).strip()


def junta_css(pasta: Path) -> None:
    """Troca cada `@import` de `css/app.css` pelo conteúdo do arquivo e apaga os demais: um CSS só no site publicado.

    Em `site/` os arquivos seguem separados por camada e componente; a junção acontece só na cópia de `docs/`.
    """
    agregador = pasta / "app.css"
    junto = RE_IMPORT.sub(
        lambda achado: (pasta / achado.group(1)).read_text(encoding="utf-8"), agregador.read_text(encoding="utf-8")
    )
    if "@import" in junto:
        sys.exit(f'ERRO: sobrou @import em {agregador}; o junta_css só entende um nível de @import url("...")')
    for caminho in sorted(pasta.rglob("*"), reverse=True):
        if caminho.is_dir():
            caminho.rmdir()
        elif caminho != agregador:
            caminho.unlink()
    agregador.write_text(enxuga_css(junto) + "\n", encoding="utf-8")


def grava_json(caminho: Path, conteudo: object) -> None:
    caminho.write_text(json.dumps(conteudo, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


NOMES_DE_MODELO = {"s5-nano": "S5-nano", "s5": "S5"}


def nome_do_modelo(modelo: str) -> str:
    """Nome de exibição: sem a etiqueta do ollama (`s5-nano:4b-q8-256k` -> `S5-nano`), igual para qualquer backend."""
    base = modelo.split(":")[0].strip()
    return NOMES_DE_MODELO.get(base.lower(), base)


def le_casas(caminho: Path | None) -> dict[str, dict[str, str]]:
    """Mapeia id do vídeo -> {"casa", "uf"} a partir do CSV de vídeos; dicionário vazio sem CSV."""
    if caminho is None:
        return {}
    casas = {}
    with caminho.open(encoding="utf-8-sig", newline="") as arquivo:
        for linha in csv.DictReader(arquivo):
            linha = {(chave or "").strip().lower(): (valor or "").strip() for chave, valor in linha.items()}
            video_id = id_do_video(linha.get("url", ""))
            if video_id:
                federal = linha.get("esfera", "").lower() == "federal"
                casas[video_id] = {
                    "casa": linha.get("orgao") or linha.get("casa") or "",
                    "uf": "" if federal else linha.get("uf", ""),
                }
    return casas


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", type=Path, help="CSV de vídeos com as colunas url, uf e orgao")
    parser.add_argument("--saida", type=Path, default=Path("docs"))
    parser.add_argument("--excluir", default="", help="ids de vídeos a deixar de fora, separados por vírgula")
    parser.add_argument("--repositorio", default=REPOSITORIO)
    parser.add_argument("--dominio", default=DOMINIO, help="domínio do GitHub Pages (vazio para não gerar CNAME)")
    args = parser.parse_args()

    modelo_do_site = Path(__file__).parent / "site"
    metadados = le_jsonl(PASTA_DADOS / "videos.jsonl")
    casas = le_casas(args.csv)
    excluidos = {video_id.strip() for video_id in args.excluir.split(",") if video_id.strip()}

    pasta_miniaturas = args.saida / "miniaturas"
    pasta_miniaturas.mkdir(parents=True, exist_ok=True)
    pasta_legendas = args.saida / "legendas"
    pasta_legendas.mkdir(parents=True, exist_ok=True)
    sessoes = []
    transcricoes = {}
    incompletos = []
    sem_miniatura = []
    for video_id, registro in tqdm(metadados.items(), desc="lendo sessões ", unit="sessão"):
        if video_id in excluidos:
            continue
        vtt = PASTA_DADOS / "transcricao" / f"{video_id}.vtt"
        texto = PASTA_DADOS / "transcricao" / f"{video_id}.txt"
        resumo = PASTA_DADOS / "resumo" / f"{video_id}.json"
        if not (vtt.exists() and texto.exists() and resumo.exists()):
            incompletos.append(video_id)
            continue
        linhas = le_transcricao(texto)
        if not linhas:
            incompletos.append(video_id)
            continue
        analise = json.loads(resumo.read_text(encoding="utf-8"))
        meta = analise.get("_meta", {})
        casa = casas.get(video_id, {})
        if not baixa_miniatura(video_id, pasta_miniaturas):
            sem_miniatura.append(video_id)
        sessoes.append(
            {
                "id": video_id,
                "titulo": registro.get("title") or video_id,
                "canal": registro.get("channel_title") or "",
                "casa": casa.get("casa") or registro.get("channel_title") or "",
                "uf": casa.get("uf", ""),
                "data": str(registro.get("started_at") or registro.get("published_at") or "")[:10],
                "duracao": registro.get("duration") or linhas[-1][0],
                "resumo": analise["resumo"],
                "tipo": analise["tipo_sessao"],
                "temas": analise["temas"],
                "palavras_chave": analise["palavras_chave"],
                "participantes": analise["participantes"],
                "decisoes": analise["decisoes"],
                "modelo": nome_do_modelo(meta.get("modelo", "")),
            }
        )
        transcricoes[video_id] = linhas
        shutil.copyfile(vtt, pasta_legendas / f"{video_id}.vtt")
    if not sessoes:
        sys.exit(f"ERRO: nenhum vídeo com pipeline completo em {PASTA_DADOS}")
    sessoes.sort(key=lambda sessao: (sessao["data"], sessao["id"]), reverse=True)

    pasta_dados = args.saida / "dados"
    pasta_dados.mkdir(parents=True, exist_ok=True)
    shutil.copytree(modelo_do_site, args.saida, dirs_exist_ok=True)
    junta_css(args.saida / "css")
    print("gravando o arquivo de busca...", flush=True)
    busca = {
        "palavras_chave": {sessao["id"]: sessao["palavras_chave"] for sessao in sessoes},
        "falas": transcricoes,
    }
    grava_json(pasta_dados / "busca.json", busca)
    config = {
        "repositorio": args.repositorio.rstrip("/"),
        "modelos": sorted({sessao["modelo"] for sessao in sessoes}),
        "sem_miniatura": sem_miniatura,
        # Tamanho do busca.json, para o site mostrar o progresso do download (o navegador só informa o comprimido).
        "busca_bytes": (pasta_dados / "busca.json").stat().st_size,
    }
    grava_json(pasta_dados / "itens.json", lista_enxuta(sessoes, config))
    # Um arquivo por sessão; a pasta é refeita para não sobrar sessão excluída. Os arquivos do formato antigo saem.
    pasta_itens = pasta_dados / "itens"
    if pasta_itens.exists():
        shutil.rmtree(pasta_itens)
    pasta_itens.mkdir()
    for sessao in tqdm(sessoes, desc="gravando itens", unit="sessão"):
        grava_json(pasta_itens / f"{sessao['id']}.json", {**sessao, "falas": transcricoes[sessao["id"]]})
    for antigo in ("indice.json", "transcricoes.json"):
        (pasta_dados / antigo).unlink(missing_ok=True)
    (args.saida / ".nojekyll").write_text("", encoding="utf-8")
    if args.dominio:
        (args.saida / "CNAME").write_text(args.dominio + "\n", encoding="utf-8")

    horas = sum(sessao["duracao"] for sessao in sessoes) / 3600
    lista = (pasta_dados / "itens.json").stat().st_size / 1024
    tamanho = (pasta_dados / "busca.json").stat().st_size / 1024 / 1024
    print(
        f"{len(sessoes)} sessões, {horas:.1f} h de vídeo, lista com {lista:.0f} KB, busca com {tamanho:.1f} MB"
        f" -> {args.saida}/"
    )
    if incompletos:
        mais = f" e outros {len(incompletos) - 10}" if len(incompletos) > 10 else ""
        print(f"{len(incompletos)} vídeos fora por pipeline incompleto: {', '.join(incompletos[:10])}{mais}")
    if excluidos:
        print(f"{len(excluidos)} vídeos excluídos a pedido: {', '.join(sorted(excluidos))}")


if __name__ == "__main__":
    main()
