"""Refaz o resumo de vídeos já transcritos em modelos da API da Maritaca, para comparar com o resumo local.

Para cada vídeo e cada modelo de `--modelos`, usa o mesmo prompt e a mesma técnica do `transcripts.py capture`
(importado daqui, então os dois arquivos precisam estar na mesma pasta) e grava `data/summary-test/<modelo>/<id>.json`. Não refaz o
que já existe. No fim, junta tudo em `data/summary-test/comparacao.json`: por vídeo, o contexto de metadados que o
modelo recebeu, a transcrição, o resumo local (`data/summaries/<id>.json`) e o de cada modelo testado.

Uma chamada por vez. A chave vem só de `MARITACA_API_KEY`.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from tqdm import tqdm

from transcripts import (
    MARITACA_URL,
    LLMError,
    MaritacaGenerator,
    analyze,
    data_path,
    read_jsonl,
    video_context,
)

PASTA_DADOS = data_path()

# Resumos avaliados em 20/11/2026: os três primeiros ficaram piores no S5-nano local quantizado; os demais, bons.
IDS_PADRAO = [
    "nBlqVDlwHWU",
    "_M8DeaEaUuo",
    "4gigyPsu2MY",
    "cek1imINtj4",
    "DldyhG7qs3I",
    "m9hVbWoPnbc",
    "XE2CCB_gcHU",
    "-8pjywnLkwI",
]


def lista_modelos(url: str) -> None:
    """Imprime os modelos que a API anuncia em `/models` (rota do padrão OpenAI; pode não existir na Maritaca)."""
    chave = os.environ.get("MARITACA_API_KEY", "").strip()
    if not chave:
        sys.exit("ERRO: defina MARITACA_API_KEY")
    requisicao = urllib.request.Request(url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {chave}"})
    try:
        with urllib.request.urlopen(requisicao, timeout=30) as resposta:
            dados = json.loads(resposta.read())
    except urllib.error.HTTPError as erro:
        sys.exit(f"ERRO: HTTP {erro.code} em /models: {erro.read().decode('utf-8', errors='replace')[:300]}")
    except (urllib.error.URLError, TimeoutError) as erro:
        sys.exit(f"ERRO: falha de rede em /models: {erro}")
    modelos = dados.get("data", dados) if isinstance(dados, dict) else dados
    for modelo in modelos:
        print(modelo.get("id", modelo) if isinstance(modelo, dict) else modelo)


def le_json(caminho: Path) -> dict | None:
    return json.loads(caminho.read_text(encoding="utf-8")) if caminho.exists() else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ids", nargs="*", default=IDS_PADRAO, help="ids dos vídeos (padrão: os 8 já avaliados)")
    parser.add_argument("--modelos", default="S5-nano,S5", help="nomes dos modelos na API, separados por vírgula")
    parser.add_argument("--maritaca-url", default=MARITACA_URL)
    parser.add_argument("--lista-modelos", action="store_true", help="só lista os modelos da API e sai")
    parser.add_argument("--max-palavras", type=int, default=8000)
    parser.add_argument("--linhas-sobrepostas", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=300, help="timeout por chamada, em segundos")
    args = parser.parse_args()

    if args.lista_modelos:
        lista_modelos(args.maritaca_url)
        return

    modelos = [modelo.strip() for modelo in args.modelos.split(",") if modelo.strip()]
    metadados = read_jsonl(PASTA_DADOS / "videos.jsonl")
    pasta_saida = PASTA_DADOS / "summary-test"
    ids = []
    for video_id in args.ids:
        if (PASTA_DADOS / "transcripts" / f"{video_id}.txt").exists():
            ids.append(video_id)
        else:
            print(f"AVISO: sem transcrição para {video_id}, ignorado")

    try:
        for modelo in modelos:
            gerador = MaritacaGenerator(args.maritaca_url, modelo, args.timeout)
            pasta = pasta_saida / re.sub(r"[^A-Za-z0-9._-]", "_", modelo)
            pasta.mkdir(parents=True, exist_ok=True)
            with tqdm(ids, desc=f"{modelo:10}", unit="vídeo") as progresso:
                for video_id in progresso:
                    destino = pasta / f"{video_id}.json"
                    if destino.exists():
                        continue
                    transcricao = (PASTA_DADOS / "transcripts" / f"{video_id}.txt").read_text(encoding="utf-8")
                    inicio = time.perf_counter()
                    try:
                        analise, info = analyze(
                            gerador,
                            transcricao,
                            metadados.get(video_id),
                            args.max_palavras,
                            args.linhas_sobrepostas,
                            progresso,
                        )
                    except LLMError as erro:
                        tqdm.write(f"ERRO em {modelo} / {video_id}: {erro}")
                        continue
                    analise["_meta"] = {
                        "video_id": video_id,
                        "backend": "maritaca",
                        "modelo": modelo,
                        "palavras": len(transcricao.split()),
                        **info,
                        "segundos": round(time.perf_counter() - inicio, 1),
                    }
                    temporario = pasta / f"{video_id}.tmp.json"
                    temporario.write_text(json.dumps(analise, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    temporario.rename(destino)
    except KeyboardInterrupt:
        print("\ninterrompido; rode de novo para continuar de onde parou")
        sys.exit(130)

    comparacao = {}
    for video_id in ids:
        comparacao[video_id] = {
            "contexto_enviado_ao_modelo": video_context(metadados.get(video_id)),
            "transcricao": (PASTA_DADOS / "transcripts" / f"{video_id}.txt").read_text(encoding="utf-8"),
            "local": le_json(PASTA_DADOS / "summaries" / f"{video_id}.json"),
            **{
                modelo: le_json(pasta_saida / re.sub(r"[^A-Za-z0-9._-]", "_", modelo) / f"{video_id}.json")
                for modelo in modelos
            },
        }
    arquivo = pasta_saida / "comparacao.json"
    pasta_saida.mkdir(parents=True, exist_ok=True)
    arquivo.write_text(json.dumps(comparacao, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"comparação gravada em {arquivo}")


if __name__ == "__main__":
    main()
