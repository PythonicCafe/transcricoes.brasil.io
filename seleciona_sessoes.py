"""Escolhe mais sessões plenárias para o pipeline, sem gastar cota da API do YouTube.

Usa o que o `canais_horas.py` já coletou (`dados/canais.jsonl`, `dados/listas/` e `dados/canais_videos.jsonl`):
filtra as sessões plenárias prováveis de cada casa (mesma heurística de título do `canais_horas.py`), por duração e
data, e escolhe em rodízio entre as casas, da mais recente para a mais antiga, até somar `--horas`.

Para cada vídeo escolhido:
- acrescenta uma linha ao CSV de vídeos (`--csv`), com as mesmas colunas que ele já tem;
- acrescenta os metadados a `dados/videos.jsonl`, para o `pipeline.py` não precisar consultar a API.

Vídeos que já estão em `dados/videos.jsonl` ou no CSV nunca são escolhidos de novo. Use `--simula` para só ver o que
seria escolhido.
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

from canais_horas import RE_EXCLUI, RE_SESSAO, le_jsonl
from pipeline import PASTA_DADOS, id_do_video

NOMES_DAS_CASAS = {"Câmara dos Deputados": "Câmara", "Senado Federal": "Senado"}


def nome_da_casa(casa: str) -> str:
    casa = casa.replace("(canal antigo)", "").strip()
    return NOMES_DAS_CASAS.get(casa, casa)


def data_do_video(video: dict) -> str:
    return str(video.get("started_at") or video.get("published_at") or "")[:10]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", type=Path, required=True, help="CSV de vídeos que receberá as novas linhas")
    parser.add_argument("--horas", type=float, required=True, help="total de horas de vídeo a acrescentar")
    parser.add_argument("--min-minutos", type=float, default=20)
    parser.add_argument("--max-minutos", type=float, default=240)
    parser.add_argument("--desde", default="2025-01-01", help="data mínima da sessão, AAAA-MM-DD")
    parser.add_argument("--simula", action="store_true", help="mostra a escolha sem gravar nada")
    args = parser.parse_args()

    canais = le_jsonl(PASTA_DADOS / "canais.jsonl")
    videos = le_jsonl(PASTA_DADOS / "canais_videos.jsonl")
    caminho_metadados = PASTA_DADOS / "videos.jsonl"
    ja_escolhidos = set(le_jsonl(caminho_metadados))
    with args.csv.open(encoding="utf-8-sig", newline="") as arquivo:
        leitor = csv.DictReader(arquivo)
        colunas = leitor.fieldnames or []
        coluna_url = next((coluna for coluna in colunas if coluna.strip().lower() == "url"), None)
        if coluna_url is None:
            sys.exit(f"ERRO: {args.csv} não tem a coluna url")
        for linha in leitor:
            video_id = id_do_video(linha.get(coluna_url) or "")
            if video_id:
                ja_escolhidos.add(video_id)

    # Candidatos por casa (os dois canais do Ceará caem na mesma casa), do mais recente para o mais antigo.
    candidatos: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for canal in canais.values():
        lista = PASTA_DADOS / "listas" / f"{canal['id']}.txt"
        if not lista.exists():
            continue
        chave = (nome_da_casa(canal["casa"]), canal["uf"])
        for video_id in lista.read_text(encoding="utf-8").split():
            video = videos.get(video_id)
            if video is None or video_id in ja_escolhidos:
                continue
            titulo = video.get("title") or ""
            minutos = (video.get("duration") or 0) / 60
            if not RE_SESSAO.search(titulo) or RE_EXCLUI.search(titulo):
                continue
            if not args.min_minutos <= minutos <= args.max_minutos or data_do_video(video) < args.desde:
                continue
            candidatos[chave].append(video)
    for lista_da_casa in candidatos.values():
        lista_da_casa.sort(key=data_do_video, reverse=True)
    if not candidatos:
        sys.exit("ERRO: nenhum candidato. Rode o canais_horas.py antes, ou afrouxe --desde e as durações.")

    escolhidos: list[tuple[tuple[str, str], dict]] = []
    segundos = 0.0
    while segundos < args.horas * 3600 and any(candidatos.values()):
        for chave in sorted(candidatos):
            if candidatos[chave] and segundos < args.horas * 3600:
                video = candidatos[chave].pop(0)
                escolhidos.append((chave, video))
                segundos += video.get("duration") or 0

    por_casa: dict[tuple[str, str], list[float]] = defaultdict(list)
    for chave, video in escolhidos:
        por_casa[chave].append((video.get("duration") or 0) / 3600)
    for (casa, uf), horas in sorted(por_casa.items()):
        print(f"{uf:2} {casa:10} {len(horas):4} vídeos {sum(horas):7.1f} h")
    print(f"total: {len(escolhidos)} vídeos, {segundos / 3600:.1f} h (pedido: {args.horas:.0f} h)")
    if args.simula:
        print("simulação: nada foi gravado")
        return

    with (
        args.csv.open("a", encoding="utf-8", newline="") as arquivo_csv,
        caminho_metadados.open("a", encoding="utf-8") as arquivo_jsonl,
    ):
        escritor = csv.DictWriter(arquivo_csv, fieldnames=colunas, extrasaction="ignore")
        for (casa, uf), video in escolhidos:
            federal = uf == "BR"
            linha = {
                "url": f"https://www.youtube.com/watch?v={video['id']}",
                "prioridade": "C",
                "minutos": round((video.get("duration") or 0) / 60),
                "esfera": "federal" if federal else "estadual",
                "uf": "DF" if federal else uf,
                "orgao": casa,
                "descricao": video.get("title") or "",
                "canal": video.get("channel_title") or "",
            }
            escritor.writerow({coluna: linha.get(coluna.strip().lower(), "") for coluna in colunas})
            arquivo_csv.flush()
            arquivo_jsonl.write(json.dumps(video, ensure_ascii=False, default=str) + "\n")
            arquivo_jsonl.flush()
    print(f"{len(escolhidos)} linhas acrescentadas a {args.csv} e a {caminho_metadados}")


if __name__ == "__main__":
    main()
