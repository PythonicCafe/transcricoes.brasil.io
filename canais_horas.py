"""Levanta quantas horas de vídeo há em cada canal legislativo do YouTube, via API (youtool).

Entrada: CSV com colunas `casa`, `uf`, `url` (canal como `/channel/<id>` ou `/@handle`).

Etapas, todas retomáveis (nada é refeito se o arquivo final já existe):
1. canais: resolve o id de cada canal e baixa título, total de vídeos e playlist de uploads -> `canais.jsonl`;
2. lista: lista os ids de todos os vídeos de cada canal -> `listas/<channel_id>.txt` (use `--atualizar-listas`
   para listar de novo e pegar vídeos novos);
3. videos: baixa duração, título e datas dos vídeos que ainda não estão em `canais_videos.jsonl` (append-only);
4. resumo: consolida por canal e por ano -> `canais_resumo.json` e `canais_resumo.csv` (sempre recalculado).

"Sessão plenária provável" é uma heurística pelo título (sessão/reunião ordinária, extraordinária, plenária ou
deliberativa, ou "plenário"; exclui solene, especial, comissão e audiência) com duração mínima de 10 minutos. Serve
para dar ordem de grandeza, não para identificar sessões com certeza.

Custo de quota da API: 1 unidade a cada 50 vídeos na listagem e mais 1 a cada 50 na coleta de detalhes.
Chave(s) em `YOUTUBE_API_KEY`, separadas por vírgula.
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from tqdm import tqdm

RE_SESSAO = re.compile(
    r"(sess[aã]o|sess[oõ]es|reuni[aã]o)\s+(plen[aá]ria|ordin[aá]ria|extraordin[aá]ria|deliberativa|n[aã]o deliberativa)"
    r"|\d+\s*[ªa]\s+(sess[aã]o|reuni[aã]o)\s+(ordin|extraordin)|plen[aá]rio",
    re.IGNORECASE,
)
RE_EXCLUI = re.compile(r"solene|especial|comiss[aã]o|audi[eê]ncia|hom?enagem", re.IGNORECASE)
MINUTOS_MINIMOS_SESSAO = 10


def le_jsonl(caminho: Path) -> dict[str, dict]:
    registros = {}
    if caminho.exists():
        with caminho.open(encoding="utf-8") as arquivo:
            for linha in arquivo:
                if linha.strip():
                    registro = json.loads(linha)
                    registros[registro["id"]] = registro
    return registros


def acrescenta(arquivo, registro: dict) -> dict:
    """Grava uma linha no JSONL (com flush) e devolve o registro já serializável."""
    linha = json.dumps(registro, ensure_ascii=False, default=str)
    arquivo.write(linha + "\n")
    arquivo.flush()
    return json.loads(linha)


def resolve_canais(youtube, linhas: list[dict], caminho: Path) -> dict[str, dict]:
    """Devolve {url: registro do canal}; só consulta a API para as URLs que faltam."""
    por_url = {registro["url"]: registro for registro in le_jsonl(caminho).values()}
    faltam = [linha for linha in linhas if linha["url"] not in por_url]
    if not faltam:
        return por_url
    with caminho.open("a", encoding="utf-8") as arquivo:
        for linha in tqdm(faltam, desc="canais", unit="canal"):
            channel_id = youtube.channel_id_from_url(linha["url"])
            if not channel_id:
                tqdm.write(f"AVISO: não achei o id do canal {linha['url']}")
                continue
            info = next(iter(youtube.channels_infos([channel_id])), None)
            if not info:
                tqdm.write(f"AVISO: a API não devolveu o canal {channel_id} ({linha['url']})")
                continue
            registro = {**info, "casa": linha["casa"], "uf": linha["uf"], "url": linha["url"]}
            registro.pop("description", None)
            por_url[linha["url"]] = acrescenta(arquivo, registro)
    return por_url


def lista_videos(youtube, canal: dict, pasta: Path, atualizar: bool) -> list[str]:
    """Ids dos vídeos da playlist de uploads; o arquivo final só aparece quando a listagem termina."""
    caminho = pasta / f"{canal['id']}.txt"
    if caminho.exists() and not atualizar:
        return caminho.read_text(encoding="utf-8").split()
    ids = []
    with tqdm(
        total=canal.get("videos") or None, desc=f"lista {canal['uf']} {canal['casa']}", unit="vídeo", leave=False
    ) as progresso:
        for video in youtube.playlist_videos(canal["playlist_id"]):
            ids.append(video["id"])
            progresso.update(1)
    temporario = pasta / f"{canal['id']}.tmp.txt"
    temporario.write_text("\n".join(ids) + "\n", encoding="utf-8")
    temporario.rename(caminho)
    return ids


def sessao_provavel(video: dict) -> bool:
    titulo = video.get("title") or ""
    minutos = (video.get("duration") or 0) / 60
    return bool(RE_SESSAO.search(titulo)) and not RE_EXCLUI.search(titulo) and minutos >= MINUTOS_MINIMOS_SESSAO


def resume(canais: list[dict], ids_por_canal: dict[str, list[str]], videos: dict[str, dict]) -> list[dict]:
    resumo = []
    for canal in canais:
        ids = ids_por_canal.get(canal["id"], [])
        coletados = [videos[video_id] for video_id in ids if video_id in videos]
        por_ano: dict[str, dict] = defaultdict(lambda: {"videos": 0, "horas": 0.0, "sessoes": 0, "horas_sessoes": 0.0})
        for video in coletados:
            ano = str(video.get("started_at") or video.get("published_at") or "")[:4] or "sem data"
            horas = (video.get("duration") or 0) / 3600
            por_ano[ano]["videos"] += 1
            por_ano[ano]["horas"] += horas
            if sessao_provavel(video):
                por_ano[ano]["sessoes"] += 1
                por_ano[ano]["horas_sessoes"] += horas
        anos = sorted(ano for ano in por_ano if ano != "sem data")
        resumo.append(
            {
                "casa": canal["casa"],
                "uf": canal["uf"],
                "canal": canal.get("title"),
                "channel_id": canal["id"],
                "url": canal["url"],
                "videos_listados": len(ids),
                "videos_com_detalhes": len(coletados),
                "horas_total": round(sum(dados["horas"] for dados in por_ano.values()), 1),
                "sessoes_provaveis": sum(dados["sessoes"] for dados in por_ano.values()),
                "horas_sessoes_provaveis": round(sum(dados["horas_sessoes"] for dados in por_ano.values()), 1),
                "primeiro_ano": anos[0] if anos else None,
                "ultimo_ano": anos[-1] if anos else None,
                "por_ano": {
                    ano: {chave: round(valor, 1) for chave, valor in dados.items()}
                    for ano, dados in sorted(por_ano.items())
                },
            }
        )
    return resumo


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv", type=Path, help="CSV com as colunas casa, uf, url")
    parser.add_argument("--dados", type=Path, default=Path("dados"))
    parser.add_argument("--atualizar-listas", action="store_true", help="lista de novo os vídeos de cada canal")
    parser.add_argument(
        "--so-resumo", action="store_true", help="não busca detalhes de vídeos; resume o que já foi coletado"
    )
    args = parser.parse_args()

    chaves = [chave.strip() for chave in os.environ.get("YOUTUBE_API_KEY", "").split(",") if chave.strip()]
    if not chaves:
        sys.exit("ERRO: defina YOUTUBE_API_KEY")
    from youtool import YouTube

    youtube = YouTube(chaves)
    with args.csv.open(encoding="utf-8-sig", newline="") as arquivo:
        linhas = [
            {chave.strip().lower(): valor.strip() for chave, valor in linha.items()}
            for linha in csv.DictReader(arquivo)
        ]
    pasta_listas = args.dados / "listas"
    pasta_listas.mkdir(parents=True, exist_ok=True)
    caminho_videos = args.dados / "canais_videos.jsonl"

    try:
        por_url = resolve_canais(youtube, linhas, args.dados / "canais.jsonl")
        canais = [por_url[linha["url"]] for linha in linhas if linha["url"] in por_url]
        ids_por_canal = {}
        for canal in tqdm(canais, desc="listas", unit="canal"):
            ids_por_canal[canal["id"]] = lista_videos(youtube, canal, pasta_listas, args.atualizar_listas)

        videos = le_jsonl(caminho_videos)
        todos = list(dict.fromkeys(video_id for ids in ids_por_canal.values() for video_id in ids))
        faltam = [video_id for video_id in todos if video_id not in videos]
        tqdm.write(f"videos: {len(todos) - len(faltam)} já coletados, {len(faltam)} a coletar")
        if args.so_resumo:
            faltam = []
        try:
            with (
                caminho_videos.open("a", encoding="utf-8") as arquivo,
                tqdm(total=len(faltam), desc="videos", unit="vídeo") as progresso,
            ):
                for registro in youtube.videos_infos(faltam):
                    registro.pop("description", None)
                    registro.pop("tags", None)
                    videos[registro["id"]] = acrescenta(arquivo, registro)
                    progresso.update(1)
        except RuntimeError as erro:
            # Cota da API esgotada (ou outro erro da API): o que já foi coletado está gravado; resume o parcial.
            tqdm.write(f"AVISO: coleta de detalhes interrompida: {str(erro)[:200]}")
    except KeyboardInterrupt:
        tqdm.write("\ninterrompido; rode de novo para continuar de onde parou")
        sys.exit(130)

    resumo = resume(canais, ids_por_canal, videos)
    (args.dados / "canais_resumo.json").write_text(
        json.dumps(resumo, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    colunas = [coluna for coluna in resumo[0] if coluna != "por_ano"] if resumo else []
    with (args.dados / "canais_resumo.csv").open("w", encoding="utf-8", newline="") as arquivo:
        escritor = csv.DictWriter(arquivo, fieldnames=colunas, extrasaction="ignore")
        escritor.writeheader()
        escritor.writerows(resumo)
    total = sum(linha["horas_total"] for linha in resumo)
    sessoes = sum(linha["horas_sessoes_provaveis"] for linha in resumo)
    listados = sum(linha["videos_listados"] for linha in resumo)
    detalhados = sum(linha["videos_com_detalhes"] for linha in resumo)
    tqdm.write(f"{len(resumo)} canais, {total:,.0f} h de vídeo, {sessoes:,.0f} h em sessões plenárias prováveis")
    if detalhados < listados:
        tqdm.write(f"PARCIAL: detalhes de {detalhados} de {listados} vídeos; as horas cobrem só esses")
    tqdm.write(f"resumo em {args.dados / 'canais_resumo.json'} e {args.dados / 'canais_resumo.csv'}")


if __name__ == "__main__":
    main()
