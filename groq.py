"""Groq Whisper transcription CLI with VAD (Voice Activity Detection).

Uses webrtcvad to detect speech segments before sending audio to the Groq API, reducing costs and avoiding
hallucinations in silent sections.

Usage:
    python groq.py transcribe audio.mp3 output.vtt
    python groq.py batch-transcribe ./audios ./transcriptions
    python groq.py transcribe audio.mp3 output.vtt --language pt --no-vad
"""

# pip install audioop-lts numpy requests pydub pytest "setuptools<82.0.0" webrtcvad
from __future__ import annotations

import argparse
import datetime
import os
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydub import AudioSegment

CHUNK_LENGTH_MS = 10 * 60 * 1000  # 10 minutes per chunk
GROQ_API_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024  # Groq upload limit
MODEL_CHOICES = ("whisper-large-v3-turbo", "whisper-large-v3")
MUSIC_SYMBOL = "♪"
MUSIC_TEXTS = (
    "ДИНАМИЧНАЯ МУЗЫКА",
    "МУЗЫКАЛЬНАЯ ЗАСТАВКА",
    "Legendas pela comunidade Amara.org",
    "Legenda Adriana Zanotto",
)
UPLOAD_FORMAT = "mp3"
UPLOAD_FILENAME = f"audio.{UPLOAD_FORMAT}"


@dataclass(frozen=True)
class TranscriptionSegment:
    start: float
    end: float
    text: str


class GroqTranscriptionError(Exception):
    pass


def to_vtt_timestamp(seconds: int | float) -> str:
    value = str(datetime.timedelta(seconds=seconds))
    if "." in value:
        value = value[: value.find(".") + 4]
    else:
        value += ".000"
    if len(value.split(":")[0]) == 1:
        value = "0" + value
    return value


def format_time(value: int | float | datetime.timedelta) -> str:
    if isinstance(value, datetime.timedelta):
        value = value.total_seconds()
    hours, remainder = divmod(value, 3600)
    minutes, seconds = divmod(remainder, 60)
    hour_str = f"{int(hours):02d}h" if hours else ""
    minute_str = f"{int(minutes):02d}m" if minutes or hours else ""
    second_str = f"{int(seconds):02d}" + f"{seconds - int(seconds):0.1f}s"[1:]
    return f"{hour_str}{minute_str}{second_str}".strip()


def segments_to_vtt(segments: list[TranscriptionSegment]) -> str:
    lines = ["WEBVTT", ""]
    for counter, seg in enumerate(segments, start=1):
        lines.append(str(counter))
        lines.append(f"{to_vtt_timestamp(seg.start)} --> {to_vtt_timestamp(seg.end)}")
        lines.append(seg.text.strip())
        lines.append("")
    return "\n".join(lines)


def detect_speech(
    audio_segment: AudioSegment,
    frame_duration: int = 30,
    min_duration: int = 100,
    vad_mode: int = 3,
    aggregate: bool = False,
    aggregate_max_gap: float = 1.0,
) -> list[tuple[float, float]]:
    """Run WebRTC VAD on `audio_segment` and return speech intervals in seconds.

    `vad_mode`: 0 (least aggressive) to 3 (most aggressive filtering).
    """
    import numpy as np
    import webrtcvad

    vad = webrtcvad.Vad(mode=vad_mode)

    audio_segment = audio_segment.set_sample_width(2)
    audio_segment = audio_segment.set_channels(1)
    audio_segment = audio_segment.set_frame_rate(16000)

    frame_length = int(audio_segment.frame_rate * (frame_duration / 1000.0))
    audio_data = np.array(audio_segment.get_array_of_samples())
    frames = [audio_data[i : i + frame_length] for i in range(0, len(audio_data), frame_length)]

    speech_segments: list[tuple[float, float]] = []
    is_speech = False
    start = 0

    for i, frame in enumerate(frames):
        if len(frame) == frame_length:
            is_speech_frame = vad.is_speech(frame.tobytes(), audio_segment.frame_rate)
            if not is_speech and is_speech_frame:
                is_speech = True
                start = i
            elif is_speech and not is_speech_frame:
                is_speech = False
                seg_start = start * frame_duration / 1000
                seg_stop = i * frame_duration / 1000
                if min_duration is None or (seg_stop - seg_start) * 1000 > min_duration:
                    speech_segments.append((seg_start, seg_stop))

    # Handle case where speech continues until the end
    if is_speech:
        seg_start = start * frame_duration / 1000
        seg_stop = len(frames) * frame_duration / 1000
        if min_duration is None or (seg_stop - seg_start) * 1000 > min_duration:
            speech_segments.append((seg_start, seg_stop))

    if aggregate:
        return aggregate_intervals(speech_segments, max_gap=aggregate_max_gap)
    return speech_segments


def aggregate_intervals(intervals: list[tuple[float, float]], max_gap: float) -> list[list[float]]:
    """Merge intervals that are separated by at most `max_gap` seconds."""
    result: list[list[float]] = []
    for start, stop in intervals:
        if not result:
            result.append([start, stop])
        else:
            last_start, last_stop = result[-1]
            if start - last_stop <= max_gap:
                result[-1][1] = stop
            else:
                result.append([start, stop])
    return result


def segment_vad_intervals(
    vad_intervals: list[list[float]], max_gap: float, min_segment: float
) -> Generator[list[list[float]], None, None]:
    """Split VAD intervals into groups suitable for individual API calls.

    Yields groups of intervals. A new group starts when the gap between consecutive intervals exceeds `max_gap` and the
    accumulated duration of the current group is at least `min_segment` seconds.
    """
    last_start, last_stop = None, None
    segment: list[list[float]] = []
    for start, stop in vad_intervals:
        if last_start is not None:
            if start - last_stop > max_gap and (segment[-1][1] - segment[0][0]) >= min_segment:
                yield segment
                segment = []
        segment.append([start, stop])
        last_start, last_stop = start, stop
    if segment:
        yield segment


def _transcribe_audio_raw(
    api_key: str,
    model: str,
    audio_data: bytes,
    filename: str = UPLOAD_FILENAME,
    language: str | None = None,
) -> list[dict]:
    """Transcribes audio using the Groq API (OpenAI-compatible endpoint)."""
    import requests

    form_data: dict[str, str] = {"model": model, "response_format": "verbose_json"}
    if language:
        form_data["language"] = language
    response = requests.post(
        GROQ_API_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": (filename, audio_data)},
        data=form_data,
        timeout=600,
    )
    if response.status_code != 200:
        raise GroqTranscriptionError(f"Groq API error: {response.status_code} - {response.text[:500]}")
    data = response.json()
    return data.get("segments", [])


def transcribe_audio(
    api_key: str,
    model: str,
    audio_data: bytes,
    filename: str = "audio.mp3",
    language: str | None = None,
) -> list[TranscriptionSegment]:
    """Transcribes audio using the Groq API (OpenAI-compatible endpoint).

    `language` is an ISO 639-1 code (e.g. "pt", "en", "es"). If provided, forces
    the Whisper model to transcribe in that language instead of auto-detecting.
    See: https://en.wikipedia.org/wiki/List_of_ISO_639-1_codes
    """
    raw = _transcribe_audio_raw(api_key, model, audio_data, filename, language)
    return [TranscriptionSegment(start=s["start"], end=s["end"], text=s["text"]) for s in raw]


def _export_audio_to_bytes(audio: AudioSegment, fmt: str = UPLOAD_FORMAT) -> bytes:
    import io

    buf = io.BytesIO()
    audio.export(buf, format=fmt, bitrate="64k")
    return buf.getvalue()


def _clean_segment_text(text: str) -> str:
    text = text.strip()
    if text in MUSIC_TEXTS:
        return MUSIC_SYMBOL
    return text


def transcribe_chunk_with_vad(
    audio: AudioSegment,
    api_key: str,
    model: str,
    language: str | None,
    time_offset: float = 0.0,
    verbose: bool = True,
) -> list[TranscriptionSegment]:
    """Transcribe a single audio chunk applying VAD to reduce cost/hallucinations.

    `time_offset` is added to every timestamp so segments refer to positions in the original (full) file.
    """
    all_vad_intervals = detect_speech(audio, aggregate=True)
    if verbose:
        intervals_str = ", ".join(f"{format_time(s)} -> {format_time(e)}" for s, e in all_vad_intervals)
        print(f"  VAD intervalos de voz detectados: {intervals_str or '(nenhum)'}")

    if not all_vad_intervals:
        if verbose:
            print("  Nenhuma atividade de voz detectada neste trecho - pulando.")
        return []

    results: list[TranscriptionSegment] = []

    for vad_intervals in segment_vad_intervals(all_vad_intervals, max_gap=3, min_segment=10):
        vad_start = vad_intervals[0][0]
        vad_stop = vad_intervals[-1][1]
        if verbose:
            print(f"  Transcrevendo sub-slice: {format_time(vad_start)} -> {format_time(vad_stop)}")

        audio_vad = audio[vad_start * 1000 : vad_stop * 1000]
        audio_bytes = _export_audio_to_bytes(audio_vad)
        raw_segments = _transcribe_audio_raw(api_key, model, audio_bytes, language=language)

        prev: TranscriptionSegment | None = None
        for seg in raw_segments:
            start = vad_start + seg["start"]
            end = vad_start + seg["end"]
            text = _clean_segment_text(seg.get("text", ""))
            if not text:
                continue

            # Filter segments outside VAD intervals (hallucination guard). Uses proper interval overlap (not just
            # point-in-interval) with a small tolerance to account for timing misalignment between WebRTC VAD and
            # Whisper.
            _tolerance = 0.5
            is_within_vad = any(start < ve + _tolerance and end > vs - _tolerance for vs, ve in vad_intervals)
            if not is_within_vad:
                if verbose:
                    print(f"    PULANDO segmento fora do VAD: {format_time(start)} -> {format_time(end)}: {text}")
                continue

            # Merge short lowercase-starting segments into the previous one
            if text[0].islower() and prev is not None and len(prev.text + " " + text) < 375:
                merged = TranscriptionSegment(
                    start=prev.start,
                    end=time_offset + end,
                    text=prev.text + " " + text,
                )
                results[-1] = merged
                prev = merged
            else:
                ts = TranscriptionSegment(
                    start=time_offset + start,
                    end=time_offset + end,
                    text=text,
                )
                results.append(ts)
                prev = ts

    return results


def transcribe_chunk_no_vad(
    audio: AudioSegment,
    api_key: str,
    model: str,
    language: str | None,
    time_offset: float = 0.0,
) -> list[TranscriptionSegment]:
    """Transcribe a single chunk without VAD (simple passthrough)."""
    audio_bytes = _export_audio_to_bytes(audio)
    raw_segments = _transcribe_audio_raw(api_key, model, audio_bytes, language=language)
    return [
        TranscriptionSegment(
            start=time_offset + s["start"],
            end=time_offset + s["end"],
            text=_clean_segment_text(s.get("text", "")),
        )
        for s in raw_segments
        if _clean_segment_text(s.get("text", ""))
    ]


def transcribe_file(
    api_key: str,
    model: str,
    audio_path: Path,
    output_path: Path,
    language: str | None = None,
    use_vad: bool = True,
    verbose: bool = True,
) -> None:
    from pydub import AudioSegment

    file_size = os.path.getsize(audio_path)
    all_segments: list[TranscriptionSegment] = []

    transcribe_fn = transcribe_chunk_with_vad if use_vad else transcribe_chunk_no_vad

    if file_size < MAX_FILE_SIZE_BYTES:
        if verbose:
            vad_label = "com VAD" if use_vad else "sem VAD"
            print(f"Arquivo pequeno ({file_size / 1024 / 1024:.2f} MB). Processando diretamente ({vad_label})...")

        audio = AudioSegment.from_file(audio_path)
        if use_vad:
            all_segments = transcribe_fn(audio, api_key, model, language, time_offset=0.0, verbose=verbose)
        else:
            all_segments = transcribe_fn(audio, api_key, model, language, time_offset=0.0)
    else:
        if verbose:
            print(f"Arquivo grande ({file_size / 1024 / 1024:.2f} MB). Iniciando fatiamento...")

        audio = AudioSegment.from_file(audio_path)
        total_duration_ms = len(audio)

        for i, start_ms in enumerate(range(0, total_duration_ms, CHUNK_LENGTH_MS)):
            end_ms = min(start_ms + CHUNK_LENGTH_MS, total_duration_ms)
            chunk = audio[start_ms:end_ms]
            time_offset = start_ms / 1000.0

            if verbose:
                print(
                    f"Processando parte {i + 1}: "
                    f"{to_vtt_timestamp(start_ms / 1000)} até {to_vtt_timestamp(end_ms / 1000)}"
                )

            if use_vad:
                segments = transcribe_fn(chunk, api_key, model, language, time_offset=time_offset, verbose=verbose)
            else:
                segments = transcribe_fn(chunk, api_key, model, language, time_offset=time_offset)
            all_segments.extend(segments)

    if verbose:
        print(f"Escrevendo saída em {output_path} ({len(all_segments)} segmentos)...")
    output_path.parent.mkdir(exist_ok=True, parents=True)
    output_path.write_text(segments_to_vtt(all_segments), encoding="utf-8")
    if verbose:
        print("Concluído.")


def _strip_vtt_tags(text: str) -> str:
    """Remove tags inline de timing do VTT (YouTube word-level captions).

    Exemplos de tags removidas: ``<00:00:01.440>``, ``<c>``, ``</c>``.
    """
    import re

    text = text.replace("<c>", "").replace("</c>", "")
    text = re.sub(r"<[\d:.]+>", "", text)
    return text.strip()


def simplify_vtt(content: str) -> str:
    """Simplifica um VTT para texto com timestamps, removendo duplicações.

    Lida com o formato de legendas automáticas do YouTube, onde cada cue repete
    o texto do cue anterior antes de acrescentar palavras novas (padrão "rolante").
    """
    result_lines: list[str] = []
    last_clean_text = ""
    current_timestamp = None

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped == "WEBVTT":
            continue
        # Headers extras do YouTube (Kind: captions, Language: en)
        if stripped.startswith("Kind:") or stripped.startswith("Language:"):
            continue
        if stripped.isdigit():
            continue
        if "-->" in stripped:
            start_time = stripped.split("-->")[0].strip()
            if "." in start_time:
                start_time = start_time.split(".")[0]
            current_timestamp = start_time
            continue

        if current_timestamp is None:
            continue

        clean = _strip_vtt_tags(stripped)
        if not clean:
            continue

        # Dedup: pula texto já emitido ou que é subconjunto do último emitido
        if clean == last_clean_text:
            continue
        if last_clean_text and last_clean_text.startswith(clean):
            continue

        result_lines.append(f"[{current_timestamp}] {clean}")
        last_clean_text = clean

    return "\n".join(result_lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcrição de áudio via Groq Whisper com suporte a VAD.",
    )
    parser.add_argument(
        "-k",
        "--api-key",
        type=str,
        default=os.environ.get("GROQ_API_KEY"),
        help="Groq API key (default: variável de ambiente GROQ_API_KEY)",
    )
    parser.add_argument(
        "-m",
        "--model",
        type=str,
        choices=MODEL_CHOICES,
        default=MODEL_CHOICES[0],
        help=f"Modelo Whisper (default: {MODEL_CHOICES[0]})",
    )
    parser.add_argument(
        "-l",
        "--language",
        type=str,
        default=None,
        help="Código ISO 639-1 do idioma (ex: pt, en, es). Se omitido, auto-detecta.",
    )
    parser.add_argument(
        "--no-vad",
        action="store_true",
        default=False,
        help="Desabilita o VAD (Voice Activity Detection).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    parser_single = subparsers.add_parser("transcribe", help="Transcreve um único arquivo de áudio")
    parser_single.add_argument("audio_filename", type=Path)
    parser_single.add_argument("output_filename", type=Path)

    parser_batch = subparsers.add_parser("batch-transcribe", help="Transcreve todos os arquivos de áudio de uma pasta")
    parser_batch.add_argument("input_dir", type=Path)
    parser_batch.add_argument("output_dir", type=Path)

    parser_simplify_vtt = subparsers.add_parser(
        "simplify-vtt", help="Simplifica um arquivo VTT, para economizar tokens"
    )
    parser_simplify_vtt.add_argument("input_filename", type=Path)
    parser_simplify_vtt.add_argument("output_filename", type=Path)

    return parser


def main() -> None:
    import sys

    parser = build_parser()
    args = parser.parse_args()

    def check_api_key():
        if args.api_key is None:
            print("ERRO: API key é obrigatória. Use -k ou defina GROQ_API_KEY.", file=sys.stderr)
            sys.exit(2)

    use_vad = not args.no_vad

    if args.command == "transcribe":
        check_api_key()
        transcribe_file(
            api_key=args.api_key,
            model=args.model,
            audio_path=args.audio_filename,
            output_path=args.output_filename,
            language=args.language,
            use_vad=use_vad,
            verbose=True,
        )

    elif args.command == "batch-transcribe":
        check_api_key()
        input_dir: Path = args.input_dir
        output_dir: Path = args.output_dir

        if not input_dir.exists():
            print(f"Erro: O diretório de entrada {input_dir} não existe.", file=sys.stderr)
            sys.exit(3)

        audio_extensions = {
            ".mp3",
            ".m4a",
            ".aac",
            ".wav",
            ".flac",
            ".ogg",
            ".opus",
            ".wma",
            ".aiff",
            ".aif",
            ".mka",
            ".mp4",
            ".webm",
            ".oga",
        }
        audio_files = sorted(
            path for path in input_dir.iterdir() if path.is_file() and path.suffix.lower() in audio_extensions
        )
        if not audio_files:
            print(f"Nenhum arquivo de áudio encontrado em {input_dir}", file=sys.stderr)
            sys.exit(4)

        print(f"Encontrados {len(audio_files)} arquivos de áudio.")

        selected_files: list[tuple[Path, Path]] = []
        for audio_file in audio_files:
            transcription_file = output_dir / audio_file.with_suffix(".vtt").name
            if transcription_file.exists():
                continue
            selected_files.append((audio_file, transcription_file))

        if not selected_files:
            print("Todos os arquivos já foram transcritos.")
            sys.exit(0)

        skipped = len(audio_files) - len(selected_files)
        if skipped == 1:
            print("1 arquivo já foi transcrito - pulando.")
        elif skipped > 1:
            print(f"{skipped} arquivos já foram transcritos - pulando.")

        for idx, (audio_file, transcription_file) in enumerate(selected_files, start=1):
            print(f"\n[{idx}/{len(selected_files)}] {audio_file.name}")
            transcribe_file(
                api_key=args.api_key,
                model=args.model,
                audio_path=audio_file,
                output_path=transcription_file,
                language=args.language,
                use_vad=use_vad,
                verbose=True,
            )

        print(f"\nBatch concluído! Arquivos salvos em: {output_dir}")

    elif args.command == "simplify-vtt":
        input_filename: Path = args.input_filename
        output_filename: Path = args.output_filename

        if not input_filename.exists():
            print(f"ERRO: arquivo {input_filename} não encontrado", file=sys.stderr)
            sys.exit(3)
        output_filename.parent.mkdir(parents=True, exist_ok=True)
        output_filename.write_text(simplify_vtt(input_filename.read_text()))


if __name__ == "__main__":
    main()


# -- Tests (pytest) --


def test_strip_vtt_tags_removes_c_tags():
    text = "Hey<00:00:00.440><c> everyone,</c><00:00:00.960><c> I'm</c>"
    assert _strip_vtt_tags(text) == "Hey everyone, I'm"


def test_strip_vtt_tags_plain_text_unchanged():
    assert _strip_vtt_tags("Hello world") == "Hello world"


def test_strip_vtt_tags_empty_and_whitespace():
    assert _strip_vtt_tags("") == ""
    assert _strip_vtt_tags("   ") == ""


def test_simplify_vtt_standard_vtt():
    """VTT simples gerado pelo Groq (sem tags <c>, sem duplicação)."""
    vtt = (
        "WEBVTT\n"
        "\n"
        "1\n"
        "00:00:00.320 --> 00:00:02.000\n"
        "Hello everyone.\n"
        "\n"
        "2\n"
        "00:00:02.000 --> 00:00:05.000\n"
        "Welcome to the talk.\n"
        "\n"
    )
    result = simplify_vtt(vtt)
    assert result == "[00:00:00] Hello everyone.\n[00:00:02] Welcome to the talk."


def test_simplify_vtt_youtube_rolling_pattern():
    """Padrão "rolante" do YouTube: cada cue repete o texto anterior + adiciona palavras novas com tags <c>."""
    vtt = (
        "WEBVTT\n"
        "Kind: captions\n"
        "Language: en\n"
        "\n"
        "00:00:00.320 --> 00:00:01.990 align:start position:0%\n"
        " \n"
        "Hey<00:00:00.440><c> everyone,</c><00:00:00.960><c> I'm</c><00:00:01.120><c> Ishaan</c>\n"
        "\n"
        "00:00:01.990 --> 00:00:02.000 align:start position:0%\n"
        "Hey everyone, I'm Ishaan\n"
        " \n"
        "\n"
        "00:00:02.000 --> 00:00:03.710 align:start position:0%\n"
        "Hey everyone, I'm Ishaan\n"
        "and<00:00:02.480><c> today</c><00:00:03.000><c> I'm</c><00:00:03.120><c> going</c>\n"
        "\n"
        "00:00:03.710 --> 00:00:03.720 align:start position:0%\n"
        "and today I'm going\n"
        " \n"
        "\n"
    )
    result = simplify_vtt(vtt)
    lines = result.splitlines()
    assert len(lines) == 2
    assert lines[0] == "[00:00:00] Hey everyone, I'm Ishaan"
    assert lines[1] == "[00:00:02] and today I'm going"


def test_simplify_vtt_no_duplicate_lines():
    """Verifica que nenhuma linha de texto aparece duplicada na saída."""
    vtt_path = Path(__file__).parent / "UPwGaM2MKHY_en.vtt"
    if not vtt_path.exists():
        import pytest

        pytest.skip("Arquivo VTT de teste não encontrado")

    result = simplify_vtt(vtt_path.read_text())
    lines = result.splitlines()

    # Nenhum texto duplicado consecutivo
    for idx in range(1, len(lines)):
        # Extrai só o texto (sem o timestamp)
        prev_text = lines[idx - 1].split("] ", 1)[1] if "] " in lines[idx - 1] else lines[idx - 1]
        curr_text = lines[idx].split("] ", 1)[1] if "] " in lines[idx] else lines[idx]
        assert curr_text != prev_text, f"Linha {idx + 1} duplicada: {curr_text!r}"


def test_simplify_vtt_no_c_tags_in_output():
    """Nenhuma tag <c> ou timestamp inline deve sobreviver na saída."""
    vtt_path = Path(__file__).parent / "UPwGaM2MKHY_en.vtt"
    if not vtt_path.exists():
        import pytest

        pytest.skip("Arquivo VTT de teste não encontrado")

    result = simplify_vtt(vtt_path.read_text())
    assert "<c>" not in result
    assert "</c>" not in result
    assert "<00:" not in result


def test_simplify_vtt_skips_youtube_headers():
    vtt = "WEBVTT\nKind: captions\nLanguage: en\n\n00:00:00.000 --> 00:00:01.000\nHello\n"
    result = simplify_vtt(vtt)
    assert "Kind:" not in result
    assert "Language:" not in result
    assert result == "[00:00:00] Hello"


def test_simplify_vtt_strips_milliseconds():
    vtt = "WEBVTT\n\n1\n00:01:23.456 --> 00:01:25.000\nTest line\n"
    result = simplify_vtt(vtt)
    assert result == "[00:01:23] Test line"


def test_simplify_vtt_real_file_line_count():
    """A saída do VTT do YouTube deve ter muito menos linhas que o VTT original (dedup efetivo)."""
    vtt_path = Path(__file__).parent / "UPwGaM2MKHY_en.vtt"
    if not vtt_path.exists():
        import pytest

        pytest.skip("Arquivo VTT de teste não encontrado")

    content = vtt_path.read_text()
    result = simplify_vtt(content)
    vtt_line_count = len([line for line in content.splitlines() if line.strip()])
    result_line_count = len(result.splitlines())

    # O VTT do YouTube tem ~3x mais cues que linhas úteis de texto (triplicação pelo padrão rolante).
    # A saída simplificada deve ter no máximo 1/3 das linhas não-vazias do VTT.
    assert result_line_count < vtt_line_count / 3, (
        f"Dedup insuficiente: {result_line_count} linhas na saída vs {vtt_line_count} no VTT"
    )
