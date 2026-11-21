"""Monta o site estático (pasta `docs/`, servida pelo GitHub Pages) a partir da saída do `pipeline.py`.

Entram só os vídeos com o pipeline completo: linha em `dados/videos.jsonl`, `dados/transcricao/<id>.vtt` e `.txt` e
`dados/resumo/<id>.json`. Arquivos temporários (`*.tmp.*`) são ignorados. Use `--excluir` para tirar vídeos do site.

Saída:
- `docs/index.html`, `docs/css/`, `docs/js/`, `docs/vendor/` e o logo: cópia de `site/`;
- `docs/legendas/<id>.vtt`: legenda de cada sessão em WebVTT, para download;
- `docs/miniaturas/<id>.jpg`: miniatura de cada vídeo, baixada do YouTube uma vez e servida pelo próprio site;
- `docs/dados/indice.json`: metadados e resumo de cada sessão;
- `docs/dados/transcricoes.json`: todas as transcrições, `{id: [[segundos, texto], ...]}`, usadas pela busca;
- `docs/CNAME` e `docs/.nojekyll`.

A busca roda no navegador, sobre todas as transcrições. Casa legislativa e UF vêm do CSV de vídeos (colunas `url`,
`uf` e `orgao`), se ele for informado; sem ele, a casa é o nome do canal.
"""

import argparse
import csv
import json
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

from pipeline import id_do_video, le_jsonl

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


def baixa_miniatura(video_id: str, pasta: Path) -> str:
    """Garante `miniaturas/<id>.jpg` e devolve o caminho relativo; se o download falhar, devolve a URL do YouTube."""
    remota = f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
    destino = pasta / f"{video_id}.jpg"
    if not destino.exists():
        try:
            with urllib.request.urlopen(remota, timeout=20) as resposta:
                conteudo = resposta.read()
        except (urllib.error.URLError, TimeoutError) as erro:
            print(f"AVISO: miniatura de {video_id} não baixada ({erro}); o site vai usar a do YouTube")
            return remota
        temporario = pasta / f"{video_id}.tmp.jpg"
        temporario.write_bytes(conteudo)
        temporario.rename(destino)
    return f"miniaturas/{video_id}.jpg"


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
    parser.add_argument("--dados", type=Path, default=Path("dados"), help="pasta de dados do pipeline")
    parser.add_argument("--csv", type=Path, help="CSV de vídeos com as colunas url, uf e orgao")
    parser.add_argument("--saida", type=Path, default=Path("docs"))
    parser.add_argument("--excluir", default="", help="ids de vídeos a deixar de fora, separados por vírgula")
    parser.add_argument("--repositorio", default=REPOSITORIO)
    parser.add_argument("--dominio", default=DOMINIO, help="domínio do GitHub Pages (vazio para não gerar CNAME)")
    args = parser.parse_args()

    modelo_do_site = Path(__file__).parent / "site"
    metadados = le_jsonl(args.dados / "videos.jsonl")
    casas = le_casas(args.csv)
    excluidos = {video_id.strip() for video_id in args.excluir.split(",") if video_id.strip()}

    pasta_miniaturas = args.saida / "miniaturas"
    pasta_miniaturas.mkdir(parents=True, exist_ok=True)
    pasta_legendas = args.saida / "legendas"
    pasta_legendas.mkdir(parents=True, exist_ok=True)
    sessoes = []
    transcricoes = {}
    incompletos = []
    for video_id, registro in metadados.items():
        if video_id in excluidos:
            continue
        vtt = args.dados / "transcricao" / f"{video_id}.vtt"
        texto = args.dados / "transcricao" / f"{video_id}.txt"
        resumo = args.dados / "resumo" / f"{video_id}.json"
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
        sessoes.append(
            {
                "id": video_id,
                "miniatura": baixa_miniatura(video_id, pasta_miniaturas),
                "legenda": f"legendas/{video_id}.vtt",
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
        sys.exit(f"ERRO: nenhum vídeo com pipeline completo em {args.dados}")
    sessoes.sort(key=lambda sessao: (sessao["data"], sessao["id"]), reverse=True)

    pasta_dados = args.saida / "dados"
    pasta_dados.mkdir(parents=True, exist_ok=True)
    shutil.copytree(modelo_do_site, args.saida, dirs_exist_ok=True)
    indice = {"config": {"repositorio": args.repositorio.rstrip("/")}, "sessoes": sessoes}
    (pasta_dados / "indice.json").write_text(json.dumps(indice, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (pasta_dados / "transcricoes.json").write_text(
        json.dumps(transcricoes, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    (args.saida / ".nojekyll").write_text("", encoding="utf-8")
    if args.dominio:
        (args.saida / "CNAME").write_text(args.dominio + "\n", encoding="utf-8")

    horas = sum(sessao["duracao"] for sessao in sessoes) / 3600
    tamanho = (pasta_dados / "transcricoes.json").stat().st_size / 1024 / 1024
    print(f"{len(sessoes)} sessões, {horas:.1f} h de vídeo, transcrições com {tamanho:.1f} MB -> {args.saida}/")
    if incompletos:
        print(f"{len(incompletos)} vídeos fora por pipeline incompleto: {', '.join(incompletos)}")
    if excluidos:
        print(f"{len(excluidos)} vídeos excluídos a pedido: {', '.join(sorted(excluidos))}")


if __name__ == "__main__":
    main()
