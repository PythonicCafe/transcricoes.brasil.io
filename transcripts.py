"""Searchable transcripts of Brazilian legislative sessions: collect, transcribe, summarize and publish.

One command line tool with a subcommand per step of the work:

    channels   count the hours of video in each legislature's YouTube channel
    select     pick more plenary sessions for the capture, without using the YouTube API
    capture    metadata (youtool) -> audio (yt-dlp) -> transcript (digue or Groq) -> summary and themes (LLM)
    site       build the static site in docs/, served by GitHub Pages
    measure    measure the built site in a real browser (time, network, memory)
    compare    compare two measurements

All pipeline data lives in `data/`, next to this file, and git ignores it. Secrets come only from environment
variables: `YOUTUBE_API_KEY` (one or more keys, comma separated), `MARITACA_API_KEY` and the Groq key that `groq.py`
reads. The keys inside the data files and the prompts sent to the model are in Portuguese, like the sessions themselves.

Exit codes: 0 on success, 1 on error, 2 on invalid arguments, 130 when interrupted with Ctrl+C.
"""

import argparse
import csv
import io
import itertools
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Pipeline data always lives here (git ignores the directory). Paths below are resolved on use, so tests can point
# DATA_DIR somewhere else.
DATA_DIR = ROOT / "data"
SITE_SOURCE_DIR = ROOT / "site"
REPOSITORY = "https://github.com/PythonicCafe/transcricoes.brasil.io"
DOMAIN = "transcricoes.brasil.io"
MARITACA_URL = "https://chat.maritaca.ai/api"
STEPS = ["metadata", "audio", "transcript", "summary"]
EXIT_INTERRUPTED = 130

# Official themes the Chamber of Deputies uses to classify bills
# (https://dadosabertos.camara.leg.br/api/v2/referencias/proposicoes/codTema). Values stay in Portuguese: they are data.
THEMES = [
    "Administração Pública",
    "Agricultura, Pecuária, Pesca e Extrativismo",
    "Arte, Cultura e Religião",
    "Cidades e Desenvolvimento Urbano",
    "Ciência, Tecnologia e Inovação",
    "Ciências Exatas e da Terra",
    "Ciências Sociais e Humanas",
    "Comunicações",
    "Defesa e Segurança",
    "Direito Civil e Processual Civil",
    "Direito Constitucional",
    "Direito e Defesa do Consumidor",
    "Direito e Justiça",
    "Direito Penal e Processual Penal",
    "Direitos Humanos e Minorias",
    "Economia",
    "Educação",
    "Energia, Recursos Hídricos e Minerais",
    "Esporte e Lazer",
    "Estrutura Fundiária",
    "Finanças Públicas e Orçamento",
    "Homenagens e Datas Comemorativas",
    "Indústria, Comércio e Serviços",
    "Meio Ambiente e Desenvolvimento Sustentável",
    "Política, Partidos e Eleições",
    "Previdência e Assistência Social",
    "Processo Legislativo e Atuação Parlamentar",
    "Relações Internacionais e Comércio Exterior",
    "Saúde",
    "Trabalho e Emprego",
    "Turismo",
    "Viação, Transporte e Mobilidade",
]

SESSION_TYPES = [
    "Plenário - sessão deliberativa",
    "Plenário - sessão de debates",
    "Comissão permanente",
    "CPI ou CPMI",
    "Conselho de Ética",
    "Audiência pública",
    "Sessão solene ou homenagem",
    "Outro",
]

SCHEMA = {
    "type": "object",
    "properties": {
        "resumo": {"type": "string"},
        "tipo_sessao": {"type": "string", "enum": SESSION_TYPES},
        "temas": {"type": "array", "items": {"type": "string", "enum": THEMES}, "minItems": 1, "maxItems": 3},
        "palavras_chave": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8},
        "participantes": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {"nome": {"type": "string"}, "papel": {"type": "string"}},
                "required": ["nome", "papel"],
            },
        },
        "decisoes": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
    },
    "required": ["resumo", "tipo_sessao", "temas", "palavras_chave", "participantes", "decisoes"],
}

INSTRUCTIONS = f"""Você analisa transcrições automáticas de vídeos de sessões públicas do Legislativo brasileiro \
(plenários, comissões, CPIs, audiências). O público são jornalistas, pesquisadores e cidadãos.

Regras:
- Use apenas o que está na transcrição. Os metadados do vídeo (título, canal, datas, descrição) servem só como \
contexto; se conflitarem com a transcrição, vale a transcrição.
- A data de publicação do vídeo pode não ser a data da sessão.
- A transcrição é automática e pode ter erros de palavras e nomes. Não invente nomes, cargos, partidos, números ou \
resultados. Se não tiver certeza de um nome, omita.
- A transcrição não diz quem está falando. Só atribua uma fala a alguém se a própria transcrição deixar claro \
(ex.: "tem a palavra o deputado X" seguido da fala).
- Seja neutro: descreva posições e argumentos sem julgá-los e sem adjetivos de opinião.

Responda SOMENTE com um objeto JSON, sem texto antes ou depois, com exatamente estes campos:
- "resumo": string. Um parágrafo de até 120 palavras com o tipo de sessão, o tema central, quem conduziu e quem \
participou (com cargo quando aparecer), as principais posições e o que foi decidido.
- "tipo_sessao": string. Uma destas opções: {"; ".join(SESSION_TYPES)}.
- "temas": lista de 1 a 3 strings, escolhidas SOMENTE desta lista (copie o texto exato), do mais para o menos \
central: {"; ".join(THEMES)}.
- "palavras_chave": lista de 3 a 8 strings com termos específicos da sessão (projetos com número, nomes de \
programas, empresas, lugares, eventos), em minúsculas, sem repetir os temas.
- "participantes": lista de até 12 objetos {{"nome": string, "papel": string}} com quem conduziu ou falou \
(ex.: papel "presidente da comissão", "deputado federal PT-SP", "convidado, diretor da empresa X").
- "decisoes": lista de strings com votações, aprovações, rejeições, requerimentos e encaminhamentos com resultado. \
Lista vazia se não houve."""

TOPIC_RULES = """Regras:
- O resumo deve ser em tópicos, um por linha, começando com "- "
- Utilize APENAS informações que estão na transcrição{and_in_summary}
- Responda APENAS com os tópicos
- Sua resposta deve ter NO MÁXIMO 8 tópicos concisos
- Preserve nomes de pessoas com cargo ou partido, números de projetos, valores, votações e decisões com resultado
- A transcrição é automática e não diz quem fala: não invente nomes nem atribua falas sem evidência no texto
- Seja neutro, sem adjetivos de opinião"""

FIRST_PART_PROMPT = """O conteúdo abaixo é a parte {part} (de {total}) da transcrição automática de um vídeo de sessão \
pública do Legislativo brasileiro. Sua tarefa é resumi-la.

{context}

{rules}

Transcrição:
-----
{transcript}
-----"""

NEXT_PART_PROMPT = """O conteúdo abaixo é a parte {part} (de {total}) da transcrição automática de um vídeo de sessão \
pública do Legislativo brasileiro. Sua tarefa é resumi-la.
Já foi feito um resumo das partes anteriores a essa, que também segue abaixo, para contextualização. Resuma APENAS \
a parte atual, sem repetir o que já está no resumo preliminar.

{context}

{rules}
- ATENÇÃO: se a transcrição não tiver pontos relevantes, sua resposta deve ser apenas "NULL" (sem aspas)

Resumo preliminar:
-----
{summary}
-----

Transcrição:
-----
{transcript}
-----"""

FINAL_FROM_TOPICS_INSTRUCTIONS = """ATENÇÃO: em vez da transcrição, você recebeu os tópicos com os principais pontos da \
sessão, em ordem cronológica, extraídos de partes consecutivas da transcrição. Onde as regras e os campos dizem \
"transcrição", leia "tópicos". Utilize APENAS informações que estão nos tópicos e NÃO REMOVA conteúdo relevante."""

# A "probable plenary session" is a guess from the title, good for orders of magnitude, not for certainty.
SESSION_RE = re.compile(
    r"(sess[aã]o|sess[oõ]es|reuni[aã]o)\s+(plen[aá]ria|ordin[aá]ria|extraordin[aá]ria|deliberativa|n[aã]o deliberativa)"
    r"|\d+\s*[ªa]\s+(sess[aã]o|reuni[aã]o)\s+(ordin|extraordin)|plen[aá]rio",
    re.IGNORECASE,
)
NOT_SESSION_RE = re.compile(r"solene|especial|comiss[aã]o|audi[eê]ncia|hom?enagem", re.IGNORECASE)
MIN_SESSION_MINUTES = 10


class CommandError(Exception):
    """An error the user can act on; `main` prints the message and exits with code 1."""


# --- shared helpers -----------------------------------------------------------------------------------------------


def status(message: str) -> None:
    """Write a status line to stderr without breaking progress bars; stdout is kept for data."""
    from tqdm import tqdm

    tqdm.write(message, file=sys.stderr)


def data_path(*parts: str) -> Path:
    return DATA_DIR.joinpath(*parts)


def video_id_from_url(url: str) -> str | None:
    """Extract the id from `watch?v=`, `youtu.be/`, `/live/`, `/shorts/` and `/embed/` URLs."""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.netloc.endswith("youtu.be"):
        return parsed.path.strip("/") or None
    query = urllib.parse.parse_qs(parsed.query)
    if "v" in query:
        return query["v"][0]
    pieces = [piece for piece in parsed.path.split("/") if piece]
    if len(pieces) >= 2 and pieces[0] in ("live", "shorts", "embed"):
        return pieces[1]
    return None


def read_jsonl(path: Path) -> dict[str, dict]:
    """Read a JSONL file into `{id: record}`; a missing file is an empty dict."""
    records = {}
    if path.exists():
        with path.open(encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    record = json.loads(line)
                    records[record["id"]] = record
    return records


def append_jsonl(file: io.TextIOBase, record: dict) -> dict:
    """Write one JSONL line (flushed right away) and return the record as it will be read back."""
    line = json.dumps(record, ensure_ascii=False, default=str)
    file.write(line + "\n")
    file.flush()
    return json.loads(line)


def write_json(path: Path, content: object) -> None:
    path.write_text(json.dumps(content, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def run_command(command: list[str], timeout: float) -> None:
    """Run a subprocess; on failure raise RuntimeError with the end of its stderr."""
    try:
        subprocess.run(command, capture_output=True, check=True, timeout=timeout)
    except subprocess.CalledProcessError as error:
        tail = (error.stderr or b"").decode("utf-8", errors="replace").strip()[-600:]
        raise RuntimeError(f"{command[0]} exited with code {error.returncode}: {tail}") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"{command[0]} exceeded the {timeout:.0f} s timeout") from error


def youtube_client():
    """YouTube API client from `YOUTUBE_API_KEY`; raises CommandError when the variable is not set."""
    keys = [key.strip() for key in os.environ.get("YOUTUBE_API_KEY", "").split(",") if key.strip()]
    if not keys:
        raise CommandError("set YOUTUBE_API_KEY to use the YouTube API")
    from youtool import YouTube

    return YouTube(keys)


def video_date(video: dict) -> str:
    return str(video.get("started_at") or video.get("published_at") or "")[:10]


def is_probable_session(video: dict) -> bool:
    title = video.get("title") or ""
    minutes = (video.get("duration") or 0) / 60
    return bool(SESSION_RE.search(title)) and not NOT_SESSION_RE.search(title) and minutes >= MIN_SESSION_MINUTES


# --- channels: hours of video per channel -------------------------------------------------------------------------


def resolve_channels(youtube, rows: list[dict], path: Path) -> dict[str, dict]:
    """Return `{url: channel record}`; only URLs missing from the file are asked to the API."""
    from tqdm import tqdm

    by_url = {record["url"]: record for record in read_jsonl(path).values()}
    missing = [row for row in rows if row["url"] not in by_url]
    if not missing:
        return by_url
    with path.open("a", encoding="utf-8") as file:
        for row in tqdm(missing, desc="channels", unit="channel", file=sys.stderr):
            channel_id = youtube.channel_id_from_url(row["url"])
            if not channel_id:
                status(f"WARNING: could not find the channel id of {row['url']}")
                continue
            info = next(iter(youtube.channels_infos([channel_id])), None)
            if not info:
                status(f"WARNING: the API did not return channel {channel_id} ({row['url']})")
                continue
            record = {**info, "casa": row["casa"], "uf": row["uf"], "url": row["url"]}
            record.pop("description", None)
            by_url[row["url"]] = append_jsonl(file, record)
    return by_url


def list_channel_videos(youtube, channel: dict, directory: Path, refresh: bool) -> list[str]:
    """Video ids of the uploads playlist; the final file only appears when the listing is complete."""
    from tqdm import tqdm

    path = directory / f"{channel['id']}.txt"
    if path.exists() and not refresh:
        return path.read_text(encoding="utf-8").split()
    ids = []
    description = f"list {channel['uf']} {channel['casa']}"
    with tqdm(
        total=channel.get("videos") or None, desc=description, unit="video", leave=False, file=sys.stderr
    ) as progress:
        for video in youtube.playlist_videos(channel["playlist_id"]):
            ids.append(video["id"])
            progress.update(1)
    temporary = directory / f"{channel['id']}.tmp.txt"
    temporary.write_text("\n".join(ids) + "\n", encoding="utf-8")
    temporary.rename(path)
    return ids


def summarize_channels(
    channels: list[dict], ids_by_channel: dict[str, list[str]], videos: dict[str, dict]
) -> list[dict]:
    """Totals per channel and per year. Keys are in Portuguese because they are the columns of the output files."""
    summary = []
    for channel in channels:
        ids = ids_by_channel.get(channel["id"], [])
        collected = [videos[video_id] for video_id in ids if video_id in videos]
        by_year: dict[str, dict] = defaultdict(lambda: {"videos": 0, "horas": 0.0, "sessoes": 0, "horas_sessoes": 0.0})
        for video in collected:
            year = video_date(video)[:4] or "sem data"
            hours = (video.get("duration") or 0) / 3600
            by_year[year]["videos"] += 1
            by_year[year]["horas"] += hours
            if is_probable_session(video):
                by_year[year]["sessoes"] += 1
                by_year[year]["horas_sessoes"] += hours
        years = sorted(year for year in by_year if year != "sem data")
        summary.append(
            {
                "casa": channel["casa"],
                "uf": channel["uf"],
                "canal": channel.get("title"),
                "channel_id": channel["id"],
                "url": channel["url"],
                "videos_listados": len(ids),
                "videos_com_detalhes": len(collected),
                "horas_total": round(sum(totals["horas"] for totals in by_year.values()), 1),
                "sessoes_provaveis": sum(totals["sessoes"] for totals in by_year.values()),
                "horas_sessoes_provaveis": round(sum(totals["horas_sessoes"] for totals in by_year.values()), 1),
                "primeiro_ano": years[0] if years else None,
                "ultimo_ano": years[-1] if years else None,
                "por_ano": {
                    year: {key: round(value, 1) for key, value in totals.items()}
                    for year, totals in sorted(by_year.items())
                },
            }
        )
    return summary


def command_channels(args: argparse.Namespace) -> int:
    from tqdm import tqdm

    youtube = youtube_client()
    with args.csv.open(encoding="utf-8-sig", newline="") as file:
        rows = [{key.strip().lower(): value.strip() for key, value in row.items()} for row in csv.DictReader(file)]
    lists_dir = data_path("lists")
    lists_dir.mkdir(parents=True, exist_ok=True)
    videos_path = data_path("channel_videos.jsonl")

    by_url = resolve_channels(youtube, rows, data_path("channels.jsonl"))
    channels = [by_url[row["url"]] for row in rows if row["url"] in by_url]
    ids_by_channel = {}
    for channel in tqdm(channels, desc="lists", unit="channel", file=sys.stderr):
        ids_by_channel[channel["id"]] = list_channel_videos(youtube, channel, lists_dir, args.update_lists)

    videos = read_jsonl(videos_path)
    every_id = list(dict.fromkeys(video_id for ids in ids_by_channel.values() for video_id in ids))
    missing = [video_id for video_id in every_id if video_id not in videos]
    status(f"videos: {len(every_id) - len(missing)} already collected, {len(missing)} to collect")
    if args.summary_only:
        missing = []
    try:
        with (
            videos_path.open("a", encoding="utf-8") as file,
            tqdm(total=len(missing), desc="videos", unit="video", file=sys.stderr) as progress,
        ):
            for record in youtube.videos_infos(missing):
                record.pop("description", None)
                record.pop("tags", None)
                videos[record["id"]] = append_jsonl(file, record)
                progress.update(1)
    except RuntimeError as error:
        # API quota exhausted (or another API error): what was collected is on disk; summarize the partial data.
        status(f"WARNING: collecting video details stopped: {str(error)[:200]}")

    summary = summarize_channels(channels, ids_by_channel, videos)
    data_path("channels_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    columns = [column for column in summary[0] if column != "por_ano"] if summary else []
    with data_path("channels_summary.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(summary)
    total = sum(row["horas_total"] for row in summary)
    sessions = sum(row["horas_sessoes_provaveis"] for row in summary)
    listed = sum(row["videos_listados"] for row in summary)
    detailed = sum(row["videos_com_detalhes"] for row in summary)
    status(f"{len(summary)} channels, {total:,.0f} h of video, {sessions:,.0f} h in probable plenary sessions")
    if detailed < listed:
        status(f"PARTIAL: details for {detailed} of {listed} videos; the hours cover only those")
    status(f"summary in {data_path('channels_summary.json')} and {data_path('channels_summary.csv')}")
    return 0


# --- select: pick more sessions -----------------------------------------------------------------------------------

HOUSE_NAMES = {"Câmara dos Deputados": "Câmara", "Senado Federal": "Senado"}


def house_name(house: str) -> str:
    """Short name used in the videos CSV; both channels of a house map to the same name."""
    house = house.replace("(canal antigo)", "").strip()
    return HOUSE_NAMES.get(house, house)


def session_candidates(
    channels: dict[str, dict],
    videos: dict[str, dict],
    lists_dir: Path,
    taken: set[str],
    min_minutes: float,
    max_minutes: float,
    since: str,
) -> dict[tuple[str, str], list[dict]]:
    """Probable sessions per (house, state), newest first, leaving out the ids in `taken`."""
    candidates: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for channel in channels.values():
        listing = lists_dir / f"{channel['id']}.txt"
        if not listing.exists():
            continue
        key = (house_name(channel["casa"]), channel["uf"])
        for video_id in listing.read_text(encoding="utf-8").split():
            video = videos.get(video_id)
            if video is None or video_id in taken:
                continue
            title = video.get("title") or ""
            minutes = (video.get("duration") or 0) / 60
            if not SESSION_RE.search(title) or NOT_SESSION_RE.search(title):
                continue
            if not min_minutes <= minutes <= max_minutes or video_date(video) < since:
                continue
            candidates[key].append(video)
    for house_candidates in candidates.values():
        house_candidates.sort(key=video_date, reverse=True)
    return candidates


def pick_round_robin(candidates: dict[tuple[str, str], list[dict]], hours: float) -> list[tuple[tuple[str, str], dict]]:
    """Take one session per house in turns until the total reaches `hours`. Consumes `candidates`."""
    chosen: list[tuple[tuple[str, str], dict]] = []
    seconds = 0.0
    while seconds < hours * 3600 and any(candidates.values()):
        for key in sorted(candidates):
            if candidates[key] and seconds < hours * 3600:
                video = candidates[key].pop(0)
                chosen.append((key, video))
                seconds += video.get("duration") or 0
    return chosen


def csv_row_for(house: str, state: str, video: dict) -> dict:
    """Row for the videos CSV; the column names are the Portuguese ones the file already uses."""
    federal = state == "BR"
    return {
        "url": f"https://www.youtube.com/watch?v={video['id']}",
        "prioridade": "C",
        "minutos": round((video.get("duration") or 0) / 60),
        "esfera": "federal" if federal else "estadual",
        "uf": "DF" if federal else state,
        "orgao": house,
        "descricao": video.get("title") or "",
        "canal": video.get("channel_title") or "",
    }


def command_select(args: argparse.Namespace) -> int:
    channels = read_jsonl(data_path("channels.jsonl"))
    videos = read_jsonl(data_path("channel_videos.jsonl"))
    metadata_path = data_path("videos.jsonl")
    taken = set(read_jsonl(metadata_path))
    with args.csv.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        columns = reader.fieldnames or []
        url_column = next((column for column in columns if column.strip().lower() == "url"), None)
        if url_column is None:
            raise CommandError(f"{args.csv} has no url column")
        for row in reader:
            video_id = video_id_from_url(row.get(url_column) or "")
            if video_id:
                taken.add(video_id)

    candidates = session_candidates(
        channels, videos, data_path("lists"), taken, args.min_minutes, args.max_minutes, args.since.isoformat()
    )
    if not candidates:
        raise CommandError("no candidates: run the channels command first, or loosen --since and the durations")
    chosen = pick_round_robin(candidates, args.hours)

    hours_by_house: dict[tuple[str, str], list[float]] = defaultdict(list)
    for key, video in chosen:
        hours_by_house[key].append((video.get("duration") or 0) / 3600)
    for (house, state), hours in sorted(hours_by_house.items()):
        print(f"{state:2} {house:10} {len(hours):4} videos {sum(hours):7.1f} h")
    total = sum(sum(hours) for hours in hours_by_house.values())
    print(f"total: {len(chosen)} videos, {total:.1f} h (asked: {args.hours:.0f} h)")
    if args.dry_run:
        status("dry run: nothing was written")
        return 0

    with (
        args.csv.open("a", encoding="utf-8", newline="") as csv_file,
        metadata_path.open("a", encoding="utf-8") as jsonl_file,
    ):
        writer = csv.DictWriter(csv_file, fieldnames=columns, extrasaction="ignore")
        for (house, state), video in chosen:
            row = csv_row_for(house, state, video)
            writer.writerow({column: row.get(column.strip().lower(), "") for column in columns})
            csv_file.flush()
            append_jsonl(jsonl_file, video)
    status(f"{len(chosen)} rows appended to {args.csv} and to {metadata_path}")
    return 0


# --- capture: metadata, audio, transcript and summary -------------------------------------------------------------
#
# Audio, transcript and summary run in parallel with each other, one thread each, ONE video at a time: while one
# audio downloads, another is transcribed and a third is summarized. There are never two downloads, two
# transcriptions or two model calls at once. No step redoes work: each skips the video whose final file exists.
# Every file is written under a temporary name and renamed when complete, so an interruption never leaves a final
# file half written.
#
# Long transcripts use "summary of the parts with context": the text is split in parts of up to `max_words`, with
# `overlap_lines` lines repeated between neighbours; each part is summarized in topics, receiving the topics of all
# previous parts as context (the answer "NULL" drops parts with nothing relevant); a last call writes the final JSON
# from the topics. A transcript that fits in one part goes straight to the final JSON. Without the model's
# tokenizer, size is estimated in words (about 1.5 token per word in Portuguese).

CHUNK_MINUTES = 30


def read_video_ids(path: Path) -> list[str]:
    """Video ids in CSV order, without repetition. Uses the `url` column, or the first one when there is none."""
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.reader(file)
        header = [name.strip().lower() for name in next(reader)]
        column = header.index("url") if "url" in header else 0
        ids = []
        for row in reader:
            if len(row) <= column or not row[column].strip():
                continue
            video_id = video_id_from_url(row[column])
            if video_id is None:
                status(f"WARNING: URL without a video id, ignored: {row[column]}")
            elif video_id not in ids:
                ids.append(video_id)
    return ids


def remove_temporaries(directory: Path, video_id: str) -> None:
    for temporary in directory.glob(f"{video_id}.tmp*"):
        temporary.unlink()


def video_minutes(metadata: dict[str, dict], video_id: str) -> float:
    return (metadata.get(video_id, {}).get("duration") or 600) / 60


def collect_metadata(ids: list[str], path: Path) -> dict[str, dict]:
    """Fetch from the YouTube API the videos missing from the JSONL file and return all records."""
    from tqdm import tqdm

    existing = read_jsonl(path)
    missing = [video_id for video_id in ids if video_id not in existing]
    if not missing:
        status(f"metadata: {len(ids)} videos already collected")
        return existing
    youtube = youtube_client()
    path.parent.mkdir(parents=True, exist_ok=True)
    with (
        path.open("a", encoding="utf-8") as file,
        tqdm(total=len(missing), desc="metadata", unit="video", file=sys.stderr) as progress,
    ):
        for record in youtube.videos_infos(missing):
            existing[record["id"]] = append_jsonl(file, record)
            progress.update(1)
    for video_id in missing:
        if video_id not in existing:
            status(f"WARNING: the API did not return video {video_id} (private, removed or wrong id)")
    return existing


def download_audio(video_id: str, directory: Path, extra_args: list[str]) -> None:
    """Download only the audio, as MP3; raise RuntimeError on failure."""
    remove_temporaries(directory, video_id)
    command = [
        "yt-dlp",
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "--format",
        "bestaudio/best",
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--output",
        str(directory / f"{video_id}.tmp.%(ext)s"),
        *extra_args,
        "--",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    try:
        run_command(command, timeout=3600)
        (directory / f"{video_id}.tmp.mp3").rename(directory / f"{video_id}.mp3")
    except (RuntimeError, OSError) as error:
        remove_temporaries(directory, video_id)
        raise RuntimeError(str(error)) from error


def vtt_seconds(mark: str) -> float:
    parts = [float(part) for part in mark.strip().split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def vtt_mark(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    return f"{hours:02d}:{minutes:02d}:{rest // 1000:02d}.{rest % 1000:03d}"


def vtt_cues(vtt: str, offset: float = 0.0) -> list[tuple[float, float, str]]:
    """Cues (start, end, text) of a WebVTT, with `offset` seconds added to the times."""
    cues: list[tuple[float, float, str]] = []
    times: tuple[float, float] | None = None
    texts: list[str] = []
    for line in [*vtt.splitlines(), ""]:
        line = line.strip()
        if "-->" in line:
            start, end = line.split("-->")[:2]
            times = (vtt_seconds(start) + offset, vtt_seconds(end.split()[0]) + offset)
            texts = []
        elif not line:
            if times and texts:
                cues.append((*times, " ".join(texts)))
            times, texts = None, []
        elif times:
            texts.append(line)
    return cues


def vtt_to_text(vtt: str) -> str:
    """Convert WebVTT to `[HH:MM:SS] text` lines, one per cue (the same format `digue convert` writes)."""
    lines = []
    for start, _end, text in vtt_cues(vtt):
        whole = int(start)
        lines.append(f"[{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d}] {text}")
    return "\n".join(lines) + "\n"


def transcribe_with_groq(audio: Path, target: Path, groq_command: list[str], directory: Path, video_id: str) -> None:
    """Cut the audio in 16 kHz mono chunks, transcribe one at a time and join everything in one VTT.

    The Groq script decodes the whole file in memory; with sessions of several hours the system kills the process
    (code -9). In 30 minute chunks each call uses a few tens of MB. Each chunk gets up to 3 attempts, to survive the
    API rate limit.
    """
    import tempfile

    with tempfile.TemporaryDirectory(dir=directory, prefix=f"{video_id}.parts.") as temporary_name:
        temporary = Path(temporary_name)
        run_command(
            [
                "ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(audio), "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "libmp3lame", "-b:a", "48k", "-f", "segment", "-segment_time", str(CHUNK_MINUTES * 60),
                "-reset_timestamps", "1", str(temporary / "%04d.mp3"),
            ],
            timeout=3600,
        )  # fmt: skip
        cues: list[tuple[float, float, str]] = []
        chunks = sorted(temporary.glob("*.mp3"))
        if not chunks:
            raise RuntimeError("ffmpeg wrote no audio chunks")
        for number, chunk in enumerate(chunks):
            chunk_vtt = chunk.with_suffix(".vtt")
            for attempt in range(1, 4):
                try:
                    run_command([*groq_command, str(chunk), str(chunk_vtt)], timeout=1200)
                    break
                except RuntimeError as error:
                    if attempt == 3:
                        raise RuntimeError(f"chunk {number + 1}/{len(chunks)}: {error}") from error
                    time.sleep(30 * attempt)
            if chunk_vtt.exists():
                cues += vtt_cues(chunk_vtt.read_text(encoding="utf-8"), number * CHUNK_MINUTES * 60)
        lines = ["WEBVTT", ""]
        for number, (start, end, text) in enumerate(cues, start=1):
            lines += [str(number), f"{vtt_mark(start)} --> {vtt_mark(end)}", text, ""]
        target.write_text("\n".join(lines), encoding="utf-8")


def transcribe(
    video_id: str,
    audio_minutes: float,
    audio_dir: Path,
    directory: Path,
    groq_command: list[str] | None = None,
    delete_audio: bool = False,
) -> None:
    """Write <id>.vtt and <id>.txt; raise RuntimeError on failure.

    Without `groq_command` it uses digue (local). With it, runs `groq_command + [audio, vtt]` (the script that calls
    the Groq API) and converts the VTT to text here, without `digue convert`. With `delete_audio`, the MP3 is removed
    only after the .txt exists, so a failure never costs a new download.
    """
    remove_temporaries(directory, video_id)
    vtt = directory / f"{video_id}.vtt"
    audio = audio_dir / f"{video_id}.mp3"
    try:
        if not vtt.exists():
            temporary_vtt = directory / f"{video_id}.tmp.vtt"
            # Generous slack: twice the audio duration, at least 30 minutes.
            limit = max(1800, audio_minutes * 60 * 2)
            if groq_command is None:
                run_command(["digue", "transcribe", "-f", "vtt", str(audio), "-o", str(temporary_vtt)], limit)
            else:
                transcribe_with_groq(audio, temporary_vtt, groq_command, directory, video_id)
            if not temporary_vtt.exists() or not temporary_vtt.stat().st_size:
                raise RuntimeError("the transcriber finished without writing the VTT")
            temporary_vtt.rename(vtt)
        temporary_text = directory / f"{video_id}.tmp.txt"
        if groq_command is None:
            run_command(["digue", "convert", str(vtt), str(temporary_text)], timeout=300)
        else:
            text = vtt_to_text(vtt.read_text(encoding="utf-8"))
            if not text.strip():
                raise RuntimeError("VTT without cues")
            temporary_text.write_text(text, encoding="utf-8")
        temporary_text.rename(directory / f"{video_id}.txt")
    except (RuntimeError, OSError) as error:
        remove_temporaries(directory, video_id)
        raise RuntimeError(str(error)) from error
    if delete_audio:
        audio.unlink(missing_ok=True)


class LLMError(Exception):
    pass


def post_json(url: str, body: dict, headers: dict[str, str], timeout: float) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json", **headers}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:400]
        raise LLMError(f"HTTP {error.code} at {url}: {detail}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise LLMError(f"network failure at {url}: {error}") from error


class OllamaGenerator:
    """Native ollama API; `format` with a JSON schema constrains the output."""

    def __init__(self, url: str, model: str, timeout: float, temperature: float) -> None:
        self.url = url.rstrip("/") + "/api/chat"
        self.model = model
        self.timeout = timeout
        self.temperature = temperature

    def generate(self, messages: list[dict], structured: bool) -> str:
        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": False,
            "options": {"num_ctx": 32768, "temperature": self.temperature, "top_p": 0.8, "top_k": 20},
        }
        if structured:
            body["format"] = SCHEMA
        return post_json(self.url, body, {}, self.timeout)["message"]["content"]


class MaritacaGenerator:
    """Maritaca API, compatible with OpenAI's (`/chat/completions`). The output format comes only from the prompt."""

    def __init__(self, url: str, model: str, timeout: float) -> None:
        key = os.environ.get("MARITACA_API_KEY", "").strip()
        if not key:
            raise CommandError("set MARITACA_API_KEY to use the maritaca backend")
        self.url = url.rstrip("/") + "/chat/completions"
        self.model = model
        self.timeout = timeout
        self.headers = {"Authorization": f"Bearer {key}"}
        self.asks_low_reasoning = True

    def generate(self, messages: list[dict], structured: bool) -> str:
        # The S5 family only accepts the default `temperature` and `top_p`, so they are not sent. The limit is high
        # because the model's reasoning, when present, spends output tokens before the answer.
        body = {"model": self.model, "messages": messages, "max_tokens": 16000}
        if self.asks_low_reasoning:
            # Without this the model sometimes spends all 16 thousand tokens reasoning and returns empty content. If
            # the API rejects the parameter, it is no longer sent in the next calls.
            try:
                response = post_json(self.url, {**body, "reasoning_effort": "low"}, self.headers, self.timeout)
            except LLMError as error:
                if "HTTP 400" not in str(error) or "reasoning" not in str(error):
                    raise
                status("  the API does not accept reasoning_effort; going on without it")
                self.asks_low_reasoning = False
                response = post_json(self.url, body, self.headers, self.timeout)
        else:
            response = post_json(self.url, body, self.headers, self.timeout)
        choice = response["choices"][0]
        content = choice["message"].get("content")
        if not content:
            raise LLMError(
                f"answer without content: finish_reason={choice.get('finish_reason')}, "
                f"message fields={sorted(choice['message'])}, usage={response.get('usage')}"
            )
        return content


def extract_json(text: str) -> dict:
    """Accept plain JSON, JSON inside a ```json block or after a <think> block."""
    text = text.split("</think>")[-1]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("answer without a JSON object")
    return json.loads(text[start : end + 1])


def validate_analysis(analysis: dict) -> dict:
    """Check fields and types and drop themes outside the official list; ValueError when no theme is left."""
    for field in SCHEMA["required"]:
        if field not in analysis:
            raise ValueError(f"missing field: {field}")
    if not isinstance(analysis["resumo"], str) or not analysis["resumo"].strip():
        raise ValueError("empty summary")
    for field in ("temas", "palavras_chave", "participantes", "decisoes"):
        if not isinstance(analysis[field], list):
            raise TypeError(f"{field} is not a list")
    analysis["temas"] = [theme for theme in analysis["temas"] if theme in THEMES][:3]
    if not analysis["temas"]:
        raise ValueError("no theme from the official list")
    if analysis["tipo_sessao"] not in SESSION_TYPES:
        analysis["tipo_sessao"] = "Outro"
    analysis["participantes"] = [
        person for person in analysis["participantes"] if isinstance(person, dict) and person.get("nome")
    ][:12]
    analysis["palavras_chave"] = [str(term) for term in analysis["palavras_chave"]][:8]
    analysis["decisoes"] = [str(decision) for decision in analysis["decisoes"]][:8]
    return {field: analysis[field] for field in SCHEMA["required"]}


def generate_validated(generator, system: str, user: str, attempts: int = 3) -> dict:
    """Ask for the final JSON; an invalid answer triggers a new attempt."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return validate_analysis(extract_json(generator.generate(messages, structured=True)))
        except (ValueError, TypeError, KeyError, LLMError) as error:
            last_error = error
            status(f"  attempt {attempt}/{attempts} failed: {error}")
            time.sleep(2 * attempt)
    raise LLMError(f"no valid answer after {attempts} attempts: {last_error}")


def generate_topics(generator, prompt: str, attempts: int = 3) -> str | None:
    """Ask for the topics of one part; None when the model answered NULL (a part with nothing relevant)."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            answer = generator.generate([{"role": "user", "content": prompt}], structured=False)
        except (KeyError, LLMError) as error:
            last_error = error
            status(f"  attempt {attempt}/{attempts} failed: {error}")
            time.sleep(2 * attempt)
            continue
        answer = answer.split("</think>")[-1].strip()
        if answer.strip('"').upper() == "NULL":
            return None
        if answer:
            return answer
        last_error = ValueError("empty answer")
    raise LLMError(f"no topics after {attempts} attempts: {last_error}")


def transcript_lines(text: str, words_per_line: int = 30) -> list[str]:
    """Non-empty lines; running text (huge lines) is rewrapped so that overlapping by lines makes sense."""
    result = []
    for line in (line.strip() for line in text.splitlines() if line.strip()):
        words = line.split()
        if len(words) <= 3 * words_per_line:
            result.append(line)
        else:
            for start in range(0, len(words), words_per_line):
                result.append(" ".join(words[start : start + words_per_line]))
    return result


def split_in_parts(lines: list[str], max_words: int, overlap_lines: int) -> list[str]:
    """Parts of up to `max_words`; each part starts by repeating the last `overlap_lines` of the previous one."""
    parts, current, words = [], [], 0
    for line in lines:
        new_words = len(line.split())
        if current and words + new_words > max_words:
            parts.append("\n".join(current))
            current = current[-overlap_lines:] if overlap_lines else []
            words = sum(len(previous.split()) for previous in current)
        current.append(line)
        words += new_words
    if current:
        parts.append("\n".join(current))
    return parts


def video_context(record: dict | None) -> str:
    """Video metadata as the model receives it (in Portuguese, like the rest of the prompt)."""
    if not record:
        return "Metadados do vídeo: não disponíveis."
    description = (record.get("description") or "").strip()
    if len(description) > 1500:
        description = description[:1500] + " [...]"
    lines = [
        "Metadados do vídeo no YouTube:",
        f"- Título: {record.get('title') or ''}",
        f"- Canal: {record.get('channel_title') or ''}",
        f"- Data de publicação: {str(record.get('published_at') or '')[:10]}",
    ]
    if record.get("started_at"):
        lines.append(f"- Início da transmissão ao vivo: {str(record['started_at'])[:16]}")
    lines.append(f"- Duração: {round((record.get('duration') or 0) / 60)} minutos")
    lines.append(f"- Descrição: {description or '(vazia)'}")
    return "\n".join(lines)


def analyze(
    generator, transcript: str, record: dict | None, max_words: int, overlap_lines: int, progress=None
) -> tuple[dict, dict]:
    """Return the final analysis and processing information (parts, dropped parts, topics)."""

    def show(text: str) -> None:
        if progress is not None:
            progress.set_postfix_str(text)

    context = video_context(record)
    parts = split_in_parts(transcript_lines(transcript), max_words, overlap_lines)
    if len(parts) == 1:
        analysis = generate_validated(generator, INSTRUCTIONS, f"{context}\n\nTranscrição:\n\n{parts[0]}")
        return analysis, {"partes": 1, "partes_sem_conteudo": 0, "topicos": None}
    topics: list[str] = []
    dropped = 0
    for number, part in enumerate(parts, start=1):
        show(f"part {number}/{len(parts)}")
        if not topics:
            prompt = FIRST_PART_PROMPT.format(
                part=number,
                total=len(parts),
                context=context,
                rules=TOPIC_RULES.format(and_in_summary=""),
                transcript=part,
            )
        else:
            prompt = NEXT_PART_PROMPT.format(
                part=number,
                total=len(parts),
                context=context,
                rules=TOPIC_RULES.format(and_in_summary=" e no resumo preliminar"),
                summary="\n".join(topics),
                transcript=part,
            )
        answer = generate_topics(generator, prompt)
        if answer is None:
            dropped += 1
        else:
            topics.append(answer)
    show("final summary")
    if not topics:
        raise LLMError("the model answered NULL for every part")
    user = f"{context}\n\nTópicos:\n-----\n" + "\n".join(topics) + "\n-----"
    analysis = generate_validated(generator, INSTRUCTIONS + "\n\n" + FINAL_FROM_TOPICS_INSTRUCTIONS, user)
    return analysis, {"partes": len(parts), "partes_sem_conteudo": dropped, "topicos": topics}


def summarize(
    video_id: str,
    record: dict | None,
    text_dir: Path,
    directory: Path,
    generator,
    backend: str,
    max_words: int,
    overlap_lines: int,
    progress=None,
) -> None:
    """Write <id>.json with summary and themes; raise RuntimeError on failure."""
    remove_temporaries(directory, video_id)
    transcript = (text_dir / f"{video_id}.txt").read_text(encoding="utf-8")
    start = time.perf_counter()
    try:
        analysis, info = analyze(generator, transcript, record, max_words, overlap_lines, progress)
    except LLMError as error:
        raise RuntimeError(str(error)) from error
    analysis["_meta"] = {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "backend": backend,
        "modelo": generator.model,
        "palavras": len(transcript.split()),
        **info,
        "segundos": round(time.perf_counter() - start, 1),
    }
    temporary = directory / f"{video_id}.tmp.json"
    temporary.write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.rename(directory / f"{video_id}.json")


END = object()


class Stage:
    """One stage of the line: a thread that handles one video at a time and hands the id to the next stage.

    `done(id)` tells whether the stage's final file already exists; `ready(id)` tells whether its input file exists;
    `work(id, progress)` does the job and raises RuntimeError when it fails.
    """

    def __init__(self, name: str, position: int, done, ready, work, max_failures_in_a_row: int | None = 3) -> None:
        self.name = name
        self.position = position
        self.done = done
        self.ready = ready
        self.work = work
        self.max_failures_in_a_row = max_failures_in_a_row
        self.inbox: queue.Queue = queue.Queue()
        self.next: Stage | None = None
        self.finished = self.skipped = self.failures = 0

    def run(self, ids: list[str], metadata: dict[str, dict], stop: threading.Event) -> None:
        from tqdm import tqdm

        pending = [video_id for video_id in ids if not self.done(video_id)]
        total = round(sum(video_minutes(metadata, video_id) for video_id in pending), 1)
        failures_in_a_row = 0
        bar_format = "{desc}: {percentage:3.0f}%|{bar}| {n:.0f}/{total:.0f} min [{elapsed}<{remaining}{postfix}]"
        with tqdm(
            total=total,
            desc=f"{self.name:10}",
            smoothing=0.1,
            position=self.position,
            bar_format=bar_format,
            file=sys.stderr,
        ) as progress:
            while not stop.is_set():
                try:
                    video_id = self.inbox.get(timeout=0.5)
                except queue.Empty:
                    continue
                if video_id is END:
                    break
                if self.done(video_id):
                    self.skipped += 1
                elif not self.ready(video_id):
                    # The previous stage failed for this video (or was not asked for and the file does not exist).
                    progress.update(round(video_minutes(metadata, video_id), 1))
                    continue
                elif self.max_failures_in_a_row is not None and failures_in_a_row >= self.max_failures_in_a_row:
                    progress.update(round(video_minutes(metadata, video_id), 1))
                    continue
                else:
                    progress.set_postfix_str(video_id)
                    try:
                        self.work(video_id, progress)
                        self.finished += 1
                        failures_in_a_row = 0
                    except RuntimeError as error:
                        self.failures += 1
                        failures_in_a_row += 1
                        status(f"ERROR in {self.name} of {video_id}: {error}")
                        if failures_in_a_row == self.max_failures_in_a_row:
                            status(f"{self.name}: {failures_in_a_row} failures in a row, stage stopped")
                    progress.update(round(video_minutes(metadata, video_id), 1))
                    progress.set_postfix_str("")
                if self.next is not None:
                    self.next.inbox.put(video_id)
        if self.next is not None:
            self.next.inbox.put(END)


def run_stages(stages: list[Stage], ids: list[str], metadata: dict[str, dict]) -> None:
    """Run the stages until every video went through all of them; KeyboardInterrupt stops the threads and is re-raised."""
    for current, following in itertools.pairwise(stages):
        current.next = following
    for video_id in ids:
        stages[0].inbox.put(video_id)
    stages[0].inbox.put(END)
    stop = threading.Event()
    threads = [threading.Thread(target=stage.run, args=(ids, metadata, stop), daemon=True) for stage in stages]
    for thread in threads:
        thread.start()
    try:
        while any(thread.is_alive() for thread in threads):
            time.sleep(0.3)
    except KeyboardInterrupt:
        stop.set()
        for thread in threads:
            thread.join(timeout=5)
        raise
    for stage in stages:
        status(f"{stage.name}: {stage.finished} done, {stage.skipped} already there, {stage.failures} with error")


def command_capture(args: argparse.Namespace) -> int:
    ids = read_video_ids(args.csv)
    status(f"{len(ids)} videos in {args.csv}")
    metadata_path = data_path("videos.jsonl")
    audio_dir = data_path("audio")
    transcript_dir = data_path("transcripts")
    summary_dir = data_path("summaries")

    metadata = collect_metadata(ids, metadata_path) if "metadata" in args.steps else read_jsonl(metadata_path)
    stages = []
    if "audio" in args.steps:
        audio_dir.mkdir(parents=True, exist_ok=True)
        extra_args = shlex.split(args.yt_dlp_args)
        stages.append(
            Stage(
                "audio",
                len(stages),
                # With --delete-audio the MP3 is gone after the transcription; a finished .txt needs no new download.
                done=lambda video_id: (
                    (audio_dir / f"{video_id}.mp3").exists() or (transcript_dir / f"{video_id}.txt").exists()
                ),
                ready=lambda video_id: True,
                work=lambda video_id, progress: download_audio(video_id, audio_dir, extra_args),
            )
        )
    if "transcript" in args.steps:
        transcript_dir.mkdir(parents=True, exist_ok=True)
        groq_command = None
        if args.transcriber == "groq":
            groq_command = [
                str(Path(part).expanduser()) if part.startswith("~") else part
                for part in shlex.split(args.groq_command)
            ]
            if len(groq_command) < 2 or not all(Path(part).exists() for part in groq_command[:2]):
                raise CommandError(f"--groq-command points to a file that does not exist: {' '.join(groq_command)}")
            groq_command += ["-m", args.groq_model, "-l", args.language, "transcribe"]
        stages.append(
            Stage(
                "transcript",
                len(stages),
                done=lambda video_id: (transcript_dir / f"{video_id}.txt").exists(),
                ready=lambda video_id: (audio_dir / f"{video_id}.mp3").exists(),
                work=lambda video_id, progress: transcribe(
                    video_id,
                    video_minutes(metadata, video_id),
                    audio_dir,
                    transcript_dir,
                    groq_command,
                    args.delete_audio,
                ),
            )
        )
    if "summary" in args.steps:
        if not args.model:
            raise CommandError("--model is required for the summary step")
        if args.backend == "ollama":
            generator = OllamaGenerator(args.ollama_url, args.model, args.timeout, args.temperature)
        else:
            generator = MaritacaGenerator(args.maritaca_url, args.model, args.timeout)
        summary_dir.mkdir(parents=True, exist_ok=True)
        stages.append(
            Stage(
                "summary",
                len(stages),
                done=lambda video_id: (summary_dir / f"{video_id}.json").exists(),
                ready=lambda video_id: (transcript_dir / f"{video_id}.txt").exists(),
                work=lambda video_id, progress: summarize(
                    video_id,
                    metadata.get(video_id),
                    transcript_dir,
                    summary_dir,
                    generator,
                    args.backend,
                    args.max_words,
                    args.overlap_lines,
                    progress,
                ),
            )
        )
    if stages:
        run_stages(stages, ids, metadata)
    return 0


# --- site: build docs/ ---------------------------------------------------------------------------------------------
#
# Only videos with the complete pipeline go in: a line in `data/videos.jsonl`, `data/transcripts/<id>.vtt` and
# `.txt`, and `data/summaries/<id>.json`. Output, all inside the output directory:
# - `index.html`, `js/`, `vendor/` and the logo: a copy of `site/`;
# - `css/app.css`: the files of `site/css/` joined in one, without comments;
# - `legendas/<id>.vtt`: captions of each session, for download;
# - `miniaturas/<id>.webp`: thumbnail of each video, downloaded from YouTube once, converted and served by the site;
# - `dados/itens.json`: slim list for the home page and the filters (rows follow `campos`; house, state, type and
#   themes are positions in the option lists);
# - `dados/busca.json`: what the search scans, `{"falas": {id: [[seconds, text], ...]}, "palavras_chave": {...}}`;
# - `dados/itens/<id>.json`: one complete session (metadata, summary, participants, decisions and lines);
# - `CNAME` and `.nojekyll`.
# The file names and JSON keys are in Portuguese because the site's JavaScript reads them.

MODEL_NAMES = {"s5-nano": "S5-nano", "s5": "S5"}
WEBP_QUALITY = 70
# Fields of each row of `itens.json`, in order; the CATEGORICAL ones and the themes go as a position in their list.
LIST_FIELDS = ["id", "titulo", "casa", "uf", "data", "duracao", "tipo", "temas", "resumo"]
CATEGORICAL = {"casa": "casas", "uf": "ufs", "tipo": "tipos"}
IMPORT_RE = re.compile(r'@import url\("([^"]+)"\);')


def mark_seconds(mark: str) -> int:
    hours, minutes, seconds = (int(part) for part in mark.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def read_transcript(path: Path) -> list[list]:
    """`[HH:MM:SS] text` lines become `[seconds, text]`; lines outside the format are ignored."""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("[") or "]" not in line:
            continue
        mark, text = line[1:].split("]", 1)
        try:
            seconds = mark_seconds(mark)
        except ValueError:
            continue
        if text.strip():
            lines.append([seconds, text.strip()])
    return lines


def display_model_name(model: str) -> str:
    """Name shown on the site: without the ollama tag (`s5-nano:4b-q8-256k` -> `S5-nano`), the same for any backend."""
    base = model.split(":")[0].strip()
    return MODEL_NAMES.get(base.lower(), base)


def read_houses(path: Path | None) -> dict[str, dict[str, str]]:
    """Map video id -> {"casa", "uf"} from the videos CSV; an empty dict without a CSV."""
    if path is None:
        return {}
    houses = {}
    with path.open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            row = {(key or "").strip().lower(): (value or "").strip() for key, value in row.items()}
            video_id = video_id_from_url(row.get("url", ""))
            if video_id:
                federal = row.get("esfera", "").lower() == "federal"
                houses[video_id] = {
                    "casa": row.get("orgao") or row.get("casa") or "",
                    "uf": "" if federal else row.get("uf", ""),
                }
    return houses


def fetch_thumbnail(video_id: str, directory: Path) -> bool:
    """Make sure `<id>.webp` exists; False when the download fails (the site then uses YouTube's thumbnail).

    YouTube serves JPEG; ffmpeg converts it to WebP, about a quarter smaller at this quality. An `<id>.jpg` left by
    an older build is converted without a new download. The JPEG is removed after the conversion.
    """
    target = directory / f"{video_id}.webp"
    if target.exists():
        return True
    original = directory / f"{video_id}.jpg"
    if not original.exists():
        try:
            with urllib.request.urlopen(f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg", timeout=20) as response:
                content = response.read()
        except (urllib.error.URLError, TimeoutError) as error:
            status(f"WARNING: thumbnail of {video_id} not downloaded ({error}); the site will use YouTube's")
            return False
        original.write_bytes(content)
    temporary = directory / f"{video_id}.tmp.webp"
    command = ["ffmpeg", "-loglevel", "error", "-y", "-i", str(original), "-c:v", "libwebp"]
    try:
        subprocess.run(
            [*command, "-quality", str(WEBP_QUALITY), str(temporary)], capture_output=True, check=True, timeout=60
        )
    except FileNotFoundError as error:
        raise CommandError("ffmpeg is not installed; it converts the thumbnails to WebP") from error
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        temporary.unlink(missing_ok=True)
        raise CommandError(f"ffmpeg could not convert the thumbnail of {video_id}: {error}") from error
    temporary.rename(target)
    original.unlink()
    return True


def slim_list(sessions: list[dict], config: dict) -> dict:
    """Build `itens.json`: only what the home page and the filters use, without repeating categorical strings."""
    options = {name: sorted({session[field] for session in sessions}) for field, name in CATEGORICAL.items()}
    options["temas"] = sorted({theme for session in sessions for theme in session["temas"]})
    positions = {name: {value: position for position, value in enumerate(values)} for name, values in options.items()}
    rows = []
    for session in sessions:
        row = []
        for field in LIST_FIELDS:
            if field in CATEGORICAL:
                row.append(positions[CATEGORICAL[field]][session[field]])
            elif field == "temas":
                row.append([positions["temas"][theme] for theme in session["temas"]])
            else:
                row.append(session[field])
        rows.append(row)
    return {"config": config, **options, "campos": LIST_FIELDS, "itens": rows}


def minify_css(css: str) -> str:
    """Drop comments and whitespace that make no difference; selectors and values are left alone."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    css = re.sub(r"\s+", " ", css)
    return re.sub(r" ?([{};]) ?", r"\1", css).strip()


def join_css(directory: Path) -> None:
    """Replace each `@import` of `css/app.css` with the file's content and delete the others: one CSS when published.

    In `site/` the files stay split by layer and component; the join happens only in the copy inside the output.
    """
    aggregator = directory / "app.css"
    joined = IMPORT_RE.sub(
        lambda match: (directory / match.group(1)).read_text(encoding="utf-8"), aggregator.read_text(encoding="utf-8")
    )
    if "@import" in joined:
        raise CommandError(f'an @import was left in {aggregator}; only one level of @import url("...") is understood')
    for path in sorted(directory.rglob("*"), reverse=True):
        if path.is_dir():
            path.rmdir()
        elif path != aggregator:
            path.unlink()
    aggregator.write_text(minify_css(joined) + "\n", encoding="utf-8")


def session_record(video_id: str, record: dict, analysis: dict, house: dict, lines: list[list]) -> dict:
    """One session as the site stores it."""
    return {
        "id": video_id,
        "titulo": record.get("title") or video_id,
        "canal": record.get("channel_title") or "",
        "casa": house.get("casa") or record.get("channel_title") or "",
        "uf": house.get("uf", ""),
        "data": video_date(record),
        "duracao": record.get("duration") or lines[-1][0],
        "resumo": analysis["resumo"],
        "tipo": analysis["tipo_sessao"],
        "temas": analysis["temas"],
        "palavras_chave": analysis["palavras_chave"],
        "participantes": analysis["participantes"],
        "decisoes": analysis["decisoes"],
        "modelo": display_model_name(analysis.get("_meta", {}).get("modelo", "")),
    }


def command_site(args: argparse.Namespace) -> int:
    from tqdm import tqdm

    metadata = read_jsonl(data_path("videos.jsonl"))
    houses = read_houses(args.csv)
    excluded = {video_id.strip() for video_id in args.exclude.split(",") if video_id.strip()}

    thumbnails_dir = args.output / "miniaturas"
    thumbnails_dir.mkdir(parents=True, exist_ok=True)
    captions_dir = args.output / "legendas"
    captions_dir.mkdir(parents=True, exist_ok=True)
    sessions = []
    transcripts = {}
    incomplete = []
    without_thumbnail = []
    for video_id, record in tqdm(metadata.items(), desc="reading sessions", unit="session", file=sys.stderr):
        if video_id in excluded:
            continue
        vtt = data_path("transcripts", f"{video_id}.vtt")
        text = data_path("transcripts", f"{video_id}.txt")
        summary = data_path("summaries", f"{video_id}.json")
        if not (vtt.exists() and text.exists() and summary.exists()):
            incomplete.append(video_id)
            continue
        lines = read_transcript(text)
        if not lines:
            incomplete.append(video_id)
            continue
        analysis = json.loads(summary.read_text(encoding="utf-8"))
        if not fetch_thumbnail(video_id, thumbnails_dir):
            without_thumbnail.append(video_id)
        sessions.append(session_record(video_id, record, analysis, houses.get(video_id, {}), lines))
        transcripts[video_id] = lines
        shutil.copyfile(vtt, captions_dir / f"{video_id}.vtt")
    if not sessions:
        raise CommandError(f"no video with the complete pipeline in {DATA_DIR}")
    sessions.sort(key=lambda session: (session["data"], session["id"]), reverse=True)

    site_data_dir = args.output / "dados"
    site_data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SITE_SOURCE_DIR, args.output, dirs_exist_ok=True)
    join_css(args.output / "css")
    status("writing the search file...")
    search = {
        "palavras_chave": {session["id"]: session["palavras_chave"] for session in sessions},
        "falas": transcripts,
    }
    write_json(site_data_dir / "busca.json", search)
    config = {
        "repositorio": args.repository.rstrip("/"),
        "modelos": sorted({session["modelo"] for session in sessions}),
        "sem_miniatura": without_thumbnail,
        # Size of busca.json, so the site can show download progress (the browser only knows the compressed size).
        "busca_bytes": (site_data_dir / "busca.json").stat().st_size,
    }
    write_json(site_data_dir / "itens.json", slim_list(sessions, config))
    # One file per session; the directory is rebuilt so no excluded session is left. Old format files are removed.
    items_dir = site_data_dir / "itens"
    if items_dir.exists():
        shutil.rmtree(items_dir)
    items_dir.mkdir()
    for session in tqdm(sessions, desc="writing items   ", unit="session", file=sys.stderr):
        write_json(items_dir / f"{session['id']}.json", {**session, "falas": transcripts[session["id"]]})
    for old in ("indice.json", "transcricoes.json"):
        (site_data_dir / old).unlink(missing_ok=True)
    (args.output / ".nojekyll").write_text("", encoding="utf-8")
    if args.domain:
        (args.output / "CNAME").write_text(args.domain + "\n", encoding="utf-8")

    hours = sum(session["duracao"] for session in sessions) / 3600
    list_size = (site_data_dir / "itens.json").stat().st_size / 1024
    search_size = (site_data_dir / "busca.json").stat().st_size / 1024 / 1024
    status(
        f"{len(sessions)} sessions, {hours:.1f} h of video, list with {list_size:.0f} KB,"
        f" search with {search_size:.1f} MB -> {args.output}/"
    )
    if incomplete:
        more = f" and {len(incomplete) - 10} more" if len(incomplete) > 10 else ""
        status(f"{len(incomplete)} videos left out, pipeline incomplete: {', '.join(incomplete[:10])}{more}")
    if excluded:
        status(f"{len(excluded)} videos excluded on request: {', '.join(sorted(excluded))}")
    return 0


# --- measure: the built site in a real browser --------------------------------------------------------------------
#
# A local server with gzip serves the site directory (like GitHub Pages does). Each scenario opens in a new browser
# context (cold cache) and records, at every step: time until the page is ready, requests, bytes transferred (with
# gzip) and decompressed, JavaScript memory after garbage collection and number of DOM nodes. Requests to other
# domains (YouTube) are blocked and counted apart: what is measured is the cost of the site itself.

PROFILES = {
    "desktop": {"viewport": {"width": 1366, "height": 768}, "network": None, "cpu": 1},
    # Simulated 4G: 9 Mbit/s down, 1.5 Mbit/s up, 170 ms of latency; CPU 4 times slower.
    "mobile": {"viewport": {"width": 390, "height": 844}, "network": (9_000_000 / 8, 1_500_000 / 8, 170), "cpu": 4},
}
COMPRESSIBLE = (".html", ".js", ".css", ".json", ".vtt", ".svg", ".txt")
MAX_WAIT = 120_000

# What is on screen, to check that two versions of the site show the same thing.
PAGE_STATE = """() => ({
  hash: location.hash,
  cards: document.querySelectorAll('.result-card').length,
  count: (document.querySelector('.result-count') || {}).textContent || '',
  passages: document.querySelectorAll('.passage-list__item').length,
  lines: document.querySelectorAll('.transcript-list__line').length,
  title: (document.querySelector('h1') || {}).textContent || '',
  first: (document.querySelector('.result-card__title') || {}).textContent || '',
  summary_length: ((document.querySelector('.machine-box p') || {}).textContent || '').length,
})"""
HOME_READY = "() => document.querySelectorAll('.result-card').length > 0"
SESSION_READY = "() => document.querySelectorAll('.transcript-list__line').length > 0"
SEARCH_READY = "() => !!document.querySelector('.result-count strong, .empty-state')"
PAGE_2_READY = "() => !!document.querySelector('.page-nav__button[aria-current=page][data-pagina=\\'2\\']')"
ABOUT_READY = "() => !!document.querySelector('.prose-block p')"


def find_browser() -> str:
    """Path of the system's Chrome or Chromium; the CHROME environment variable points to another executable."""
    return os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome") or ""


def static_handler(directory: Path):
    """Request handler class for static files with gzip for text, HTTP/1.1 with keep-alive."""
    import functools
    import gzip
    import http.server

    compressed: dict[str, bytes] = {}

    class StaticHandler(http.server.SimpleHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            path = Path(self.translate_path(self.path.split("?")[0].split("#")[0]))
            if path.is_dir():
                path = path / "index.html"
            if not path.is_file():
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", self.guess_type(str(path)))
            if path.suffix in COMPRESSIBLE and "gzip" in self.headers.get("Accept-Encoding", ""):
                key = str(path)
                if key not in compressed:
                    compressed[key] = gzip.compress(body, compresslevel=6)
                body = compressed[key]
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=600")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *arguments) -> None:
            pass

    return functools.partial(StaticHandler, directory=str(directory))


class Meter:
    """Drive one page through a scenario, taking a snapshot of requests and metrics at every step."""

    def __init__(self, page, origin: str, profile: dict) -> None:
        self.page = page
        self.origin = origin
        self.cdp = page.context.new_cdp_session(page)
        self.cdp.send("Network.enable")
        self.cdp.send("Performance.enable")
        self.cdp.send("HeapProfiler.enable")
        if profile["network"]:
            download, upload, latency = profile["network"]
            self.cdp.send(
                "Network.emulateNetworkConditions",
                {"offline": False, "downloadThroughput": download, "uploadThroughput": upload, "latency": latency},
            )
        if profile["cpu"] > 1:
            self.cdp.send("Emulation.setCPUThrottlingRate", {"rate": profile["cpu"]})
        self.urls: dict[str, str] = {}
        self.finished: list[tuple[str, int]] = []
        self.decompressed = 0
        self.blocked = 0
        self.steps: list[dict] = []
        self.cdp.on(
            "Network.requestWillBeSent",
            lambda event: self.urls.__setitem__(event["requestId"], event["request"]["url"]),
        )
        self.cdp.on("Network.loadingFinished", self._on_finished)
        self.cdp.on("Network.dataReceived", self._on_data)
        page.route("**/*", self._route)

    def _route(self, route) -> None:
        if route.request.url.startswith(self.origin):
            route.continue_()
        else:
            self.blocked += 1
            route.abort()

    def _on_finished(self, event: dict) -> None:
        url = self.urls.get(event["requestId"], "")
        if url.startswith(self.origin):
            self.finished.append((url[len(self.origin) :], int(event["encodedDataLength"])))

    def _on_data(self, event: dict) -> None:
        if self.urls.get(event["requestId"], "").startswith(self.origin):
            self.decompressed += int(event["dataLength"])

    def open(self, name: str, page_hash: str, ready: str) -> None:
        """Step: load the site at `page_hash`."""
        self.step(name, lambda: self.page.goto(f"{self.origin}/{page_hash}", wait_until="commit"), ready)

    def open_home(self) -> None:
        self.open("home", "", HOME_READY)

    def click(self, name: str, selector: str, ready: str) -> None:
        """Step: click the first element matching `selector`."""
        self.step(
            name, lambda: self.page.evaluate("(selector) => document.querySelector(selector).click()", selector), ready
        )

    def open_first_session(self) -> None:
        self.click("open the first session", ".result-card__title a", SESSION_READY)

    def search(self, query: str) -> None:
        """Step: type a query and wait for its results."""

        def action() -> None:
            self.page.fill("#consulta", query)
            self.page.press("#consulta", "Enter")
            # The loading state has no <strong>; the final count or the empty notice do.
            self.page.wait_for_function(
                "(query) => { const strong = document.querySelector('.result-count strong');"
                " return (strong && strong.textContent === query) || !!document.querySelector('.empty-state'); }",
                arg=query,
                timeout=MAX_WAIT,
            )

        self.step(f"search {query}", action, SEARCH_READY)

    def filter(self, house: str, theme: str, since: str) -> None:
        """Step: choose a house, a theme and a start date in the home filters."""

        def action() -> None:
            self.page.evaluate(
                """([house, theme, since]) => {
                    const box = [...document.querySelectorAll('#casas input')].find((item) => item.value === house);
                    box.checked = true;
                    document.getElementById('tema').value = theme;
                    document.getElementById('de').value = since;
                    document.getElementById('de').dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                [house, theme, since],
            )

        self.step("filter house, theme and date", action, HOME_READY)

    def step(self, name: str, action, ready: str) -> None:
        """Run `action`, wait for the JavaScript expression `ready` to be true and record what changed."""
        requests_before = len(self.finished)
        transferred_before = sum(size for _, size in self.finished)
        decompressed_before = self.decompressed
        start = time.perf_counter()
        action()
        self.page.wait_for_function(ready, timeout=MAX_WAIT)
        duration = time.perf_counter() - start
        self.page.wait_for_load_state("networkidle", timeout=MAX_WAIT)
        self.cdp.send("HeapProfiler.collectGarbage")
        metrics = {item["name"]: item["value"] for item in self.cdp.send("Performance.getMetrics")["metrics"]}
        new = self.finished[requests_before:]
        self.steps.append(
            {
                "step": name,
                "seconds": round(duration, 3),
                "requests": len(new),
                "transferred": sum(size for _, size in new),
                "decompressed": self.decompressed - decompressed_before,
                "js_memory": int(metrics["JSHeapUsedSize"]),
                "dom_nodes": int(metrics["Nodes"]),
                "files": [url for url, _ in new],
                "transferred_total": transferred_before + sum(size for _, size in new),
                "state": self.page.evaluate(PAGE_STATE),
            }
        )


def scenarios(session: str) -> dict:
    """Each scenario takes a Meter and runs its steps; `session` is the id used in direct links."""

    def home_only(meter: Meter) -> None:
        meter.open_home()

    def open_session(meter: Meter) -> None:
        meter.open_home()
        meter.open_first_session()

    def filters_then_session(meter: Meter) -> None:
        meter.open_home()
        meter.filter("Câmara", "Saúde", "2025-01-01")
        meter.open_first_session()

    def one_search(meter: Meter) -> None:
        meter.open_home()
        meter.search("orçamento")

    def search_then_passage(meter: Meter) -> None:
        meter.open_home()
        meter.search("orçamento")
        meter.click("open the first passage", ".passage-list .time-link", SESSION_READY)

    def three_searches(meter: Meter) -> None:
        meter.open_home()
        for query in ("orçamento", "saúde", "emenda parlamentar"):
            meter.search(query)

    def direct_session(meter: Meter) -> None:
        meter.open("session", f"#/v/{session}", SESSION_READY)

    def direct_passage(meter: Meter) -> None:
        meter.open("session at minute 10", f"#/v/{session}/600", SESSION_READY)

    def about_page(meter: Meter) -> None:
        meter.open_home()
        meter.click("about", "a[href='#/sobre']", ABOUT_READY)

    def second_page_then_session(meter: Meter) -> None:
        meter.open_home()
        meter.click("go to page 2", "[data-pagina='2']", PAGE_2_READY)
        meter.open_first_session()

    return {
        "1. Home only": home_only,
        "2. Home > open a session": open_session,
        "3. Home > filters > open a session": filters_then_session,
        "4. Home > search": one_search,
        "5. Home > search > open a passage": search_then_passage,
        "6. Home > three searches": three_searches,
        "7. Direct link to a session": direct_session,
        "8. Direct link to a passage": direct_passage,
        "9. Home > about page": about_page,
        "10. Home > page 2 > open a session": second_page_then_session,
    }


def median_session(site: Path) -> str:
    """Id of the session whose JSON has the median size, so direct links measure a typical one."""
    sizes = sorted((path.stat().st_size, path.stem) for path in (site / "dados" / "itens").glob("*.json"))
    if not sizes:
        raise CommandError(f"{site}/dados/itens/ is empty; build the site first or pass --session")
    return sizes[len(sizes) // 2][1]


def command_measure(args: argparse.Namespace) -> int:
    browser_path = find_browser()
    if not browser_path:
        raise CommandError("no Chrome or Chromium found; install one or set the CHROME environment variable")
    session = args.session or median_session(args.site)
    import http.server
    import statistics

    from playwright.sync_api import sync_playwright

    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), static_handler(args.site))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{args.port}"
    result = {"site": str(args.site), "repetitions": args.repetitions, "session": session, "profiles": {}}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=browser_path, args=["--no-sandbox"])
            for profile_name in args.profiles:
                profile = PROFILES[profile_name]
                result["profiles"][profile_name] = {}
                for scenario_name, run_scenario in scenarios(session).items():
                    runs = []
                    for _ in range(args.repetitions):
                        context = browser.new_context(viewport=profile["viewport"], locale="pt-BR")
                        meter = Meter(context.new_page(), origin, profile)
                        run_scenario(meter)
                        runs.append({"steps": meter.steps, "blocked": meter.blocked})
                        context.close()
                    steps = []
                    for position, step in enumerate(runs[0]["steps"]):
                        median = dict(step)
                        for field in ("seconds", "js_memory"):
                            median[field] = statistics.median(run["steps"][position][field] for run in runs)
                        steps.append(median)
                    result["profiles"][profile_name][scenario_name] = {"steps": steps, "blocked": runs[0]["blocked"]}
                    line = " | ".join(
                        f"{step['step']}: {step['seconds']:.2f} s, {step['requests']} req,"
                        f" {step['transferred'] / 1024:.0f} KB"
                        for step in steps
                    )
                    status(f"[{profile_name}] {scenario_name}: {line}")
            browser.close()
    finally:
        server.shutdown()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    status(f"written to {args.output}")
    return 0


# --- compare: two measurements -------------------------------------------------------------------------------------


def format_size(amount: float) -> str:
    return f"{amount / 2**20:.2f} MB" if amount >= 2**20 else f"{amount / 2**10:.0f} KB"


def comparison_lines(before: dict, after: dict) -> list[str]:
    """Markdown tables comparing two measurements, one per profile, plus the steps where the screen differed."""
    lines = []
    differences = []
    for profile, scenario_results in before["profiles"].items():
        lines += [
            "",
            f"## {profile}",
            "",
            (
                "| Scenario | Step | Time before | Time after | Network before | Network after | JS memory before"
                " | JS memory after |"
            ),
            "|---|---|---|---|---|---|---|---|",
        ]
        for scenario, measured in scenario_results.items():
            other = after["profiles"].get(profile, {}).get(scenario)
            if other is None:
                continue
            for step_before, step_after in zip(measured["steps"], other["steps"], strict=True):
                lines.append(
                    f"| {scenario} | {step_before['step']} | {step_before['seconds']:.2f} s"
                    f" | {step_after['seconds']:.2f} s | {format_size(step_before['transferred'])}"
                    f" | {format_size(step_after['transferred'])} | {format_size(step_before['js_memory'])}"
                    f" | {format_size(step_after['js_memory'])} |"
                )
                if step_before["state"] != step_after["state"]:
                    differences.append(f"  [{profile}] {scenario} | {step_before['step']}")
            time_before = sum(step["seconds"] for step in measured["steps"])
            time_after = sum(step["seconds"] for step in other["steps"])
            lines.append(
                f"| {scenario} | **total** | **{time_before:.2f} s** | **{time_after:.2f} s**"
                f" | **{format_size(measured['steps'][-1]['transferred_total'])}**"
                f" | **{format_size(other['steps'][-1]['transferred_total'])}** | | |"
            )
    for profile, scenario_results in after["profiles"].items():
        for scenario, measured in scenario_results.items():
            if scenario not in before["profiles"].get(profile, {}):
                for step in measured["steps"]:
                    lines.append(
                        f"only in the second [{profile}] {scenario} | {step['step']} | {step['seconds']:.2f} s"
                        f" | {format_size(step['transferred'])} | {format_size(step['js_memory'])}"
                    )
    lines += ["", f"Steps where the screen showed something different: {len(differences)}", *differences]
    return lines


def command_compare(args: argparse.Namespace) -> int:
    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    print("\n".join(comparison_lines(before, after)))
    return 0


# --- command line --------------------------------------------------------------------------------------------------


def iso_date(value: str):
    """argparse type: YYYY-MM-DD -> datetime.date."""
    import datetime

    try:
        return datetime.date.fromisoformat(value.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date: {value!r} (expected: YYYY-MM-DD)") from None


def step_list(value: str) -> list[str]:
    """argparse type: comma separated steps of the capture."""
    steps = [step.strip() for step in value.split(",") if step.strip()]
    for step in steps:
        if step not in STEPS:
            raise argparse.ArgumentTypeError(f"unknown step: {step!r}. Options: {', '.join(STEPS)}")
    return steps


def profile_list(value: str) -> list[str]:
    """argparse type: comma separated measurement profiles."""
    profiles = [profile.strip() for profile in value.split(",") if profile.strip()]
    for profile in profiles:
        if profile not in PROFILES:
            raise argparse.ArgumentTypeError(f"unknown profile: {profile!r}. Options: {', '.join(PROFILES)}")
    return profiles


def _configure_channels(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-u", "--update-lists", action="store_true", help="List the videos of each channel again")
    parser.add_argument(
        "-s",
        "--summary-only",
        action="store_true",
        help="Do not fetch video details; summarize what was already collected",
    )
    parser.add_argument("csv", type=Path, help="CSV with the columns casa, uf and url (channel URL)")


def _configure_select(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-m", "--min-minutes", metavar="minutes", type=float, default=20, help="Shortest session (default: 20)"
    )
    parser.add_argument(
        "-M", "--max-minutes", metavar="minutes", type=float, default=240, help="Longest session (default: 240)"
    )
    parser.add_argument(
        "-s",
        "--since",
        metavar="date",
        type=iso_date,
        default=iso_date("2025-01-01"),
        help="Oldest session date, YYYY-MM-DD (default: 2025-01-01)",
    )
    parser.add_argument("-n", "--dry-run", action="store_true", help="Show the choice without writing anything")
    parser.add_argument("csv", type=Path, help="Videos CSV that receives the new rows")
    parser.add_argument("hours", type=float, help="Total hours of video to add")


def _configure_capture(parser: argparse.ArgumentParser) -> None:
    transcribers = ("digue", "groq")
    backends = ("ollama", "maritaca")
    default_groq = shlex.join([sys.executable, str(ROOT / "groq.py")])
    parser.add_argument(
        "-s",
        "--steps",
        metavar="steps",
        type=step_list,
        default=STEPS,
        help=f"Comma separated steps to run (default: all). Options: {', '.join(STEPS)}",
    )
    parser.add_argument(
        "-y",
        "--yt-dlp-args",
        metavar="args",
        default="",
        help='Extra options for yt-dlp, e.g. "--cookies-from-browser firefox"',
    )
    parser.add_argument(
        "-t",
        "--transcriber",
        metavar="name",
        choices=transcribers,
        default="digue",
        help=f"Local digue or the Groq API script (default: digue). Options: {', '.join(transcribers)}",
    )
    parser.add_argument(
        "--groq-command",
        metavar="command",
        default=default_groq,
        help="Interpreter and Groq script (default: this Python and the repository's groq.py)",
    )
    parser.add_argument("--groq-model", metavar="model", default="whisper-large-v3", help="Groq transcription model")
    parser.add_argument(
        "-l", "--language", metavar="code", default="pt", help="Language given to the Groq script (default: pt)"
    )
    parser.add_argument(
        "-d", "--delete-audio", action="store_true", help="Delete the MP3 after transcribing, to save disk space"
    )
    parser.add_argument(
        "-b",
        "--backend",
        metavar="name",
        choices=backends,
        default="ollama",
        help=f"Where the summary model runs (default: ollama). Options: {', '.join(backends)}",
    )
    parser.add_argument(
        "-m", "--model", metavar="model", help="Model name in the chosen backend (needed for the summary step)"
    )
    parser.add_argument("--ollama-url", metavar="url", default="http://localhost:11434", help="ollama address")
    parser.add_argument("--maritaca-url", metavar="url", default=MARITACA_URL, help="Maritaca API address")
    parser.add_argument(
        "-w",
        "--max-words",
        metavar="words",
        type=int,
        default=8000,
        help="Size of each part of a long transcript (default: 8000, about 12 thousand tokens)",
    )
    parser.add_argument(
        "-o",
        "--overlap-lines",
        metavar="lines",
        type=int,
        default=10,
        help="Lines repeated between neighbouring parts (default: 10)",
    )
    parser.add_argument(
        "-T", "--temperature", metavar="value", type=float, default=0.3, help="Model temperature, ollama only"
    )
    parser.add_argument(
        "--timeout", metavar="seconds", type=float, default=1800, help="Timeout of each model call (default: 1800)"
    )
    parser.add_argument("csv", type=Path, help="CSV with a url column")


def _configure_site(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-c", "--csv", metavar="file", type=Path, help="Videos CSV with the columns url, uf and orgao")
    parser.add_argument(
        "-o", "--output", metavar="dir", type=Path, default=Path("docs"), help="Output directory (default: docs)"
    )
    parser.add_argument("-x", "--exclude", metavar="ids", default="", help="Comma separated video ids to leave out")
    parser.add_argument("-r", "--repository", metavar="url", default=REPOSITORY, help="Repository linked by the site")
    parser.add_argument(
        "-d", "--domain", metavar="domain", default=DOMAIN, help="GitHub Pages domain (empty to write no CNAME)"
    )


def _configure_measure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-r", "--repetitions", metavar="n", type=int, default=3, help="Runs per scenario; times are the median"
    )
    parser.add_argument(
        "-p",
        "--profiles",
        metavar="names",
        type=profile_list,
        default=list(PROFILES),
        help=f"Comma separated profiles (default: all). Options: {', '.join(PROFILES)}",
    )
    parser.add_argument(
        "-s", "--session", metavar="id", default="", help="Session used in direct links (default: the median one)"
    )
    parser.add_argument("-P", "--port", metavar="port", type=int, default=8765, help="Local port (default: 8765)")
    parser.add_argument("site", type=Path, help="Directory to serve (the built docs/)")
    parser.add_argument("output", type=Path, help="JSON file to write")


def _configure_compare(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("before", type=Path, help="First measurement")
    parser.add_argument("after", type=Path, help="Second measurement")


COMMANDS = {
    "channels": (
        "Count the hours of video in each legislature's YouTube channel",
        _configure_channels,
        command_channels,
    ),
    "select": (
        "Pick more plenary sessions for the capture, without the YouTube API",
        _configure_select,
        command_select,
    ),
    "capture": ("Download, transcribe and summarize the videos of a CSV", _configure_capture, command_capture),
    "site": ("Build the static site from the captured data", _configure_site, command_site),
    "measure": ("Measure the built site in a real browser", _configure_measure, command_measure),
    "compare": ("Compare two measurements and print Markdown tables", _configure_compare, command_compare),
}


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transcripts",
        description="Collect, transcribe, summarize and publish Brazilian legislative sessions",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="command", required=True)
    for name, (description, configure, _run) in COMMANDS.items():
        configure(subparsers.add_parser(name, help=description, description=description))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = create_parser().parse_args(argv)
    try:
        return COMMANDS[args.command][2](args)
    except CommandError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # Final files only exist when complete; temporaries (*.tmp.*) are cleaned on the next run.
        print("\ninterrupted; run again to continue from where it stopped", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
