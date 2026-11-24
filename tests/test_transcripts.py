"""Tests for transcripts.py. Nothing here touches the network: API clients, models and commands are faked."""

import json
import threading

import pytest

import transcripts


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """An empty data directory used instead of the repository's `data/`."""
    directory = tmp_path / "data"
    directory.mkdir()
    monkeypatch.setattr(transcripts, "DATA_DIR", directory)
    return directory


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(transcripts.time, "sleep", lambda seconds: None)


def write_jsonl(path, records):
    path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")


def doc(video_id):
    """Document id of a YouTube video."""
    return transcripts.document_id(transcripts.youtube_url(video_id))


def youtube_record(video_id, **fields):
    """A line of `data/videos.jsonl`: YouTube metadata with the document's uuid and url."""
    return transcripts.document_from_youtube({"id": video_id, **fields})


VALID_ANALYSIS = {
    "resumo": "Sessão deliberativa sobre o orçamento.",
    "tipo_sessao": "Plenário - sessão deliberativa",
    "temas": ["Finanças Públicas e Orçamento"],
    "palavras_chave": ["pl 123"],
    "participantes": [{"nome": "Fulano", "papel": "presidente"}],
    "decisoes": ["aprovado o requerimento"],
}


class FakeGenerator:
    """Answers model calls from a list, recording the prompts it received."""

    model = "fake-model"

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def generate(self, messages, structured):
        self.calls.append((messages, structured))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


# --- shared helpers -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=abc123DEF45", "abc123DEF45"),
        ("https://www.youtube.com/watch?t=10&v=abc123DEF45", "abc123DEF45"),
        ("https://youtu.be/abc123DEF45", "abc123DEF45"),
        ("https://www.youtube.com/live/abc123DEF45?feature=share", "abc123DEF45"),
        ("https://www.youtube.com/shorts/abc123DEF45", "abc123DEF45"),
        ("https://www.youtube.com/embed/abc123DEF45", "abc123DEF45"),
        ("  https://youtu.be/abc123DEF45  ", "abc123DEF45"),
        ("https://www.youtube.com/@camara", None),
        ("", None),
    ],
)
def test_video_id_from_url(url, expected):
    assert transcripts.video_id_from_url(url) == expected


def test_jsonl_round_trip_keeps_the_last_record_per_id(tmp_path):
    path = tmp_path / "videos.jsonl"
    with path.open("a", encoding="utf-8") as file:
        returned = transcripts.append_jsonl(file, {"id": "a", "title": "Sessão", "when": transcripts.Path("x")})
        transcripts.append_jsonl(file, {"id": "b", "title": "old"})
        transcripts.append_jsonl(file, {"id": "b", "title": "new"})
    assert returned == {"id": "a", "title": "Sessão", "when": "x"}
    records = transcripts.read_jsonl(path)
    assert list(records) == ["a", "b"]
    assert records["b"]["title"] == "new"
    assert "Sessão" in path.read_text(encoding="utf-8")


def test_read_jsonl_by_another_key_needs_it_in_every_line(tmp_path):
    path = tmp_path / "videos.jsonl"
    write_jsonl(path, [youtube_record("aaa")])
    assert list(transcripts.read_jsonl(path, key="uuid")) == [doc("aaa")]
    write_jsonl(path, [{"id": "aaa"}])
    with pytest.raises(transcripts.CommandError, match="line 1: record without 'uuid'"):
        transcripts.read_jsonl(path, key="uuid")


def test_document_id_is_the_uuid5_of_the_canonical_url():
    # The same id DocSearch gives this video: uuid5(NAMESPACE_URL, "https://youtube.com/watch?v=<id>").
    assert transcripts.youtube_url("dQw4w9WgXcQ") == "https://youtube.com/watch?v=dQw4w9WgXcQ"
    assert transcripts.document_id("https://youtube.com/watch?v=dQw4w9WgXcQ") == "56a157e6-37b5-55a6-bbbc-807e2e86de5f"


def test_document_from_youtube_puts_uuid_and_url_in_front():
    record = transcripts.document_from_youtube({"id": "dQw4w9WgXcQ", "title": "x"})
    assert list(record) == ["uuid", "url", "id", "title"]
    assert record["uuid"] == "56a157e6-37b5-55a6-bbbc-807e2e86de5f"


@pytest.mark.parametrize(
    "text",
    [
        "56a157e6-37b5-55a6-bbbc-807e2e86de5f",
        " 56A157E637B555A6BBBC807E2E86DE5F ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "dQw4w9WgXcQ",
    ],
)
def test_document_id_from_accepts_uuids_urls_and_video_ids(text):
    assert transcripts.document_id_from(text) == "56a157e6-37b5-55a6-bbbc-807e2e86de5f"


def test_read_jsonl_of_a_missing_file_is_empty(tmp_path):
    assert transcripts.read_jsonl(tmp_path / "nothing.jsonl") == {}


def test_run_command_reports_the_end_of_stderr():
    with pytest.raises(RuntimeError, match="exited with code 3: boom"):
        transcripts.run_command(["sh", "-c", "echo boom >&2; exit 3"], timeout=10)


def test_run_command_reports_a_timeout():
    with pytest.raises(RuntimeError, match="exceeded the 0 s timeout"):
        transcripts.run_command(["sleep", "5"], timeout=0.2)


def test_youtube_client_needs_the_key(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    with pytest.raises(transcripts.CommandError, match="YOUTUBE_API_KEY"):
        transcripts.youtube_client()


@pytest.mark.parametrize(
    ("title", "minutes", "expected"),
    [
        ("Sessão Ordinária - 12/03/2025", 90, True),
        ("45ª Sessão Extraordinária", 30, True),
        ("Plenário - votação do orçamento", 120, True),
        ("Sessão Ordinária", 5, False),
        ("Sessão Solene em homenagem", 90, False),
        ("Comissão de Finanças - reunião ordinária", 90, False),
        ("Audiência pública", 90, False),
        ("Podcast da Assembleia", 90, False),
    ],
)
def test_is_probable_session(title, minutes, expected):
    assert transcripts.is_probable_session({"title": title, "duration": minutes * 60}) is expected


# --- channels -----------------------------------------------------------------------------------------------------


def test_summarize_channels_counts_hours_and_probable_sessions():
    channels = [{"id": "c1", "casa": "Câmara", "uf": "BR", "title": "Câmara dos Deputados", "url": "u"}]
    videos = {
        "v1": {"id": "v1", "title": "Sessão Ordinária", "duration": 7200, "published_at": "2024-05-01T10:00:00"},
        "v2": {"id": "v2", "title": "Podcast", "duration": 3600, "started_at": "2025-01-02T10:00:00"},
    }
    (row,) = transcripts.summarize_channels(channels, {"c1": ["v1", "v2", "not-collected"]}, videos)
    assert row["videos_listados"] == 3
    assert row["videos_com_detalhes"] == 2
    assert row["horas_total"] == 3.0
    assert row["sessoes_provaveis"] == 1
    assert row["horas_sessoes_provaveis"] == 2.0
    assert (row["primeiro_ano"], row["ultimo_ano"]) == ("2024", "2025")
    assert row["por_ano"]["2025"] == {"videos": 1, "horas": 1.0, "sessoes": 0, "horas_sessoes": 0.0}


# --- select -------------------------------------------------------------------------------------------------------


def session_video(video_id, date, minutes=60, title="Sessão Ordinária"):
    return {"id": video_id, "title": title, "duration": minutes * 60, "published_at": f"{date}T10:00:00"}


@pytest.fixture
def selection_data(data_dir, tmp_path):
    """Two houses (one with two channels) with collected videos, and a videos CSV that already has one of them."""
    write_jsonl(
        data_dir / "channels.jsonl",
        [
            {"id": "ch-camara", "casa": "Câmara dos Deputados", "uf": "BR", "url": "u1"},
            {"id": "ch-ce", "casa": "Alece", "uf": "CE", "url": "u2"},
            {"id": "ch-ce-old", "casa": "Alece (canal antigo)", "uf": "CE", "url": "u3"},
        ],
    )
    videos = [
        session_video("cam-new", "2025-06-01"),
        session_video("cam-old", "2025-02-01"),
        session_video("cam-2024", "2024-12-01"),
        session_video("cam-short", "2025-05-01", minutes=5),
        session_video("cam-solemn", "2025-05-02", title="Sessão Solene"),
        session_video("cam-taken", "2025-07-01"),
        session_video("ce-new", "2025-03-01", minutes=120),
        session_video("ce-older-channel", "2025-01-15"),
    ]
    write_jsonl(data_dir / "channel_videos.jsonl", videos)
    (data_dir / "lists").mkdir()
    (data_dir / "lists" / "ch-camara.txt").write_text(
        "cam-new\ncam-old\ncam-2024\ncam-short\ncam-solemn\ncam-taken\nnot-collected\n", encoding="utf-8"
    )
    (data_dir / "lists" / "ch-ce.txt").write_text("ce-new\n", encoding="utf-8")
    (data_dir / "lists" / "ch-ce-old.txt").write_text("ce-older-channel\n", encoding="utf-8")
    csv_path = tmp_path / "videos.csv"
    csv_path.write_text(
        "url,prioridade,minutos,esfera,uf,orgao,descricao,canal\n"
        "https://www.youtube.com/watch?v=cam-taken,A,60,federal,DF,Câmara,x,y\n",
        encoding="utf-8",
    )
    return csv_path


def test_house_name_merges_channels_of_the_same_house():
    assert transcripts.house_name("Câmara dos Deputados") == "Câmara"
    assert transcripts.house_name("Alece (canal antigo)") == "Alece"
    assert transcripts.house_name("ALBA") == "ALBA"


def test_select_dry_run_prints_the_choice_and_writes_nothing(selection_data, data_dir, capsys):
    before = selection_data.read_text(encoding="utf-8")
    assert transcripts.main(["select", "--dry-run", str(selection_data), "100"]) == 0
    output = capsys.readouterr().out
    assert "total: 4 videos, 5.0 h" in output
    assert selection_data.read_text(encoding="utf-8") == before
    assert not (data_dir / "videos.jsonl").exists()


def test_select_appends_rows_in_turns_newest_first(selection_data, data_dir):
    assert transcripts.main(["select", str(selection_data), "2.5"]) == 0
    rows = selection_data.read_text(encoding="utf-8").splitlines()[2:]
    # One per house in turns (Alece before Câmara, alphabetically), stopping when the hours are reached.
    assert [row.split(",")[0].split("=")[1] for row in rows] == ["ce-new", "cam-new"]
    assert rows[0].split(",")[3:6] == ["estadual", "CE", "Alece"]
    assert rows[1].split(",")[3:6] == ["federal", "DF", "Câmara"]
    records = transcripts.read_jsonl(data_dir / "videos.jsonl", key="uuid")
    assert list(records) == [doc("ce-new"), doc("cam-new")]
    assert records[doc("ce-new")]["url"] == "https://youtube.com/watch?v=ce-new"


def test_select_never_picks_the_same_video_twice(selection_data):
    transcripts.main(["select", str(selection_data), "100"])
    first = selection_data.read_text(encoding="utf-8")
    assert transcripts.main(["select", str(selection_data), "100"]) == 1
    assert selection_data.read_text(encoding="utf-8") == first


def test_select_respects_since_and_durations(selection_data, capsys):
    transcripts.main(["select", "-n", "--since", "2024-01-01", "--min-minutes", "1", str(selection_data), "100"])
    assert "total: 6 videos" in capsys.readouterr().out


def test_select_without_url_column_is_an_error(data_dir, tmp_path, capsys):
    csv_path = tmp_path / "videos.csv"
    csv_path.write_text("link\nx\n", encoding="utf-8")
    assert transcripts.main(["select", str(csv_path), "10"]) == 1
    assert "has no url column" in capsys.readouterr().err


# --- capture ------------------------------------------------------------------------------------------------------

VTT = """WEBVTT

1
00:00:01.500 --> 00:00:04.000
Declaro aberta
a sessão.

2
01:02:03.250 --> 01:02:05.000
Tem a palavra o deputado.
"""


def test_read_documents_uses_the_url_column_without_repeating(tmp_path):
    path = tmp_path / "videos.csv"
    path.write_text(
        "orgao,URL\nCâmara,https://youtu.be/aaa\nSenado,https://www.youtube.com/watch?v=bbb\n"
        "Câmara,https://www.youtube.com/live/aaa\nx,\ny,https://example.com/nothing\n",
        encoding="utf-8",
    )
    assert transcripts.read_documents(path) == {
        doc("aaa"): "https://youtube.com/watch?v=aaa",
        doc("bbb"): "https://youtube.com/watch?v=bbb",
    }


def test_read_documents_falls_back_to_the_first_column(tmp_path):
    path = tmp_path / "videos.csv"
    path.write_text("link\nhttps://youtu.be/aaa\n", encoding="utf-8")
    assert list(transcripts.read_documents(path)) == [doc("aaa")]


def test_vtt_times():
    assert transcripts.vtt_seconds("01:02:03.250") == 3723.25
    assert transcripts.vtt_seconds("02:03.5") == 123.5
    assert transcripts.vtt_mark(3723.25) == "01:02:03.250"
    assert transcripts.vtt_mark(0) == "00:00:00.000"


def test_vtt_cues_join_lines_and_apply_the_offset():
    assert transcripts.vtt_cues(VTT, offset=1800) == [
        (1801.5, 1804.0, "Declaro aberta a sessão."),
        (5523.25, 5525.0, "Tem a palavra o deputado."),
    ]


def test_vtt_to_text_writes_one_line_per_cue():
    assert transcripts.vtt_to_text(VTT) == "[00:00:01] Declaro aberta a sessão.\n[01:02:03] Tem a palavra o deputado.\n"


def test_transcript_lines_rewraps_only_huge_lines():
    lines = transcripts.transcript_lines("curta\n\n" + " ".join(["palavra"] * 100) + "\n", words_per_line=30)
    assert lines[0] == "curta"
    assert [len(line.split()) for line in lines[1:]] == [30, 30, 30, 10]


def test_split_in_parts_overlaps_lines_between_parts():
    lines = [f"linha {number} com cinco palavras" for number in range(6)]
    parts = transcripts.split_in_parts(lines, max_words=10, overlap_lines=1)
    assert [part.splitlines() for part in parts] == [lines[0:2], lines[1:3], lines[2:4], lines[3:5], lines[4:6]]
    assert transcripts.split_in_parts(lines, max_words=10, overlap_lines=0)[1].splitlines() == lines[2:4]
    assert transcripts.split_in_parts(lines, max_words=1000, overlap_lines=5) == ["\n".join(lines)]


def test_extract_json_accepts_fences_and_thinking():
    assert transcripts.extract_json('<think>{"x": 0}</think>\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError, match="without a JSON object"):
        transcripts.extract_json("no json here")


def test_validate_analysis_cleans_what_the_model_got_wrong():
    analysis = {
        **VALID_ANALYSIS,
        "tipo_sessao": "Algo inventado",
        "temas": ["Tema inventado", "Saúde", "Educação", "Economia", "Turismo"],
        "participantes": [{"nome": "Fulano", "papel": "x"}, {"papel": "sem nome"}, "texto"],
        "palavras_chave": [1, "dois"],
        "extra": "dropped",
    }
    cleaned = transcripts.validate_analysis(analysis)
    assert cleaned["tipo_sessao"] == "Outro"
    assert cleaned["temas"] == ["Saúde", "Educação", "Economia"]
    assert cleaned["participantes"] == [{"nome": "Fulano", "papel": "x"}]
    assert cleaned["palavras_chave"] == ["1", "dois"]
    assert "extra" not in cleaned


@pytest.mark.parametrize(
    ("change", "error", "message"),
    [
        ({"temas": ["Tema inventado"]}, ValueError, "no theme from the official list"),
        ({"resumo": "  "}, ValueError, "empty summary"),
        ({"decisoes": "nenhuma"}, TypeError, "decisoes is not a list"),
    ],
)
def test_validate_analysis_rejects_unusable_answers(change, error, message):
    with pytest.raises(error, match=message):
        transcripts.validate_analysis({**VALID_ANALYSIS, **change})


def test_validate_analysis_requires_every_field():
    with pytest.raises(ValueError, match="missing field: decisoes"):
        transcripts.validate_analysis({key: value for key, value in VALID_ANALYSIS.items() if key != "decisoes"})


def test_analyze_short_transcript_is_one_structured_call():
    generator = FakeGenerator([json.dumps(VALID_ANALYSIS)])
    analysis, info = transcripts.analyze(generator, "[00:00:01] texto curto", {"title": "Sessão X"}, 8000, 10)
    assert analysis == VALID_ANALYSIS
    assert info == {"partes": 1, "partes_sem_conteudo": 0, "topicos": None}
    ((messages, structured),) = generator.calls
    assert structured is True
    assert "Título: Sessão X" in messages[1]["content"]
    assert "texto curto" in messages[1]["content"]


def test_analyze_long_transcript_summarizes_parts_with_context():
    transcript = "\n".join(f"linha {number} com cinco palavras" for number in range(6))
    generator = FakeGenerator(["- tópico da parte 1", "NULL", "- tópico da parte 3", json.dumps(VALID_ANALYSIS)])
    analysis, info = transcripts.analyze(generator, transcript, None, max_words=10, overlap_lines=0)
    assert analysis == VALID_ANALYSIS
    assert info == {
        "partes": 3,
        "partes_sem_conteudo": 1,
        "topicos": ["- tópico da parte 1", "- tópico da parte 3"],
    }
    prompts = [messages[-1]["content"] for messages, _structured in generator.calls]
    assert "parte 1 (de 3)" in prompts[0]
    assert "Resumo preliminar" not in prompts[0]
    assert "- tópico da parte 1" in prompts[2]
    assert "linha 0" not in prompts[3]
    assert "- tópico da parte 3" in prompts[3]
    assert [structured for _messages, structured in generator.calls] == [False, False, False, True]


def test_analyze_fails_when_every_part_is_null():
    transcript = "\n".join(f"linha {number} com cinco palavras" for number in range(4))
    with pytest.raises(transcripts.LLMError, match="NULL for every part"):
        transcripts.analyze(FakeGenerator(["NULL", '"null"']), transcript, None, max_words=10, overlap_lines=0)


def test_generate_validated_retries_invalid_answers(no_sleep):
    generator = FakeGenerator(["not json", transcripts.LLMError("HTTP 500"), json.dumps(VALID_ANALYSIS)])
    assert transcripts.generate_validated(generator, "system", "user") == VALID_ANALYSIS
    assert len(generator.calls) == 3


def test_generate_validated_gives_up_after_the_attempts(no_sleep):
    with pytest.raises(transcripts.LLMError, match="no valid answer after 3 attempts"):
        transcripts.generate_validated(FakeGenerator(["x", "y", "z"]), "system", "user")


def test_summarize_writes_the_json_with_processing_details(tmp_path):
    text_dir, summary_dir = tmp_path / "text", tmp_path / "summary"
    text_dir.mkdir()
    summary_dir.mkdir()
    (text_dir / "vid.txt").write_text("[00:00:01] uma fala qualquer\n", encoding="utf-8")
    (summary_dir / "vid.tmp.json").write_text("leftover", encoding="utf-8")
    record = {"url": "https://youtube.com/watch?v=x"}
    transcripts.summarize(
        "vid", record, text_dir, summary_dir, FakeGenerator([json.dumps(VALID_ANALYSIS)]), "ollama", 8000, 10
    )
    written = json.loads((summary_dir / "vid.json").read_text(encoding="utf-8"))
    assert written["resumo"] == VALID_ANALYSIS["resumo"]
    assert (written["_meta"]["uuid"], written["_meta"]["url"]) == ("vid", "https://youtube.com/watch?v=x")
    assert written["_meta"]["modelo"] == "fake-model"
    assert written["_meta"]["backend"] == "ollama"
    assert written["_meta"]["palavras"] == 4
    assert [path.name for path in summary_dir.iterdir()] == ["vid.json"]


def test_summarize_turns_model_failures_into_runtime_errors(tmp_path, no_sleep):
    (tmp_path / "vid.txt").write_text("[00:00:01] fala\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="no valid answer"):
        transcripts.summarize("vid", None, tmp_path, tmp_path, FakeGenerator(["x", "y", "z"]), "ollama", 8000, 10)
    assert not (tmp_path / "vid.json").exists()


def test_transcribe_with_groq_writes_text_and_deletes_the_audio_when_asked(tmp_path, monkeypatch):
    audio_dir, directory = tmp_path / "audio", tmp_path / "transcripts"
    audio_dir.mkdir()
    directory.mkdir()
    (audio_dir / "vid.mp3").write_bytes(b"mp3")
    monkeypatch.setattr(
        transcripts,
        "transcribe_with_groq",
        lambda audio, target, command, directory, video_id: target.write_text(VTT, encoding="utf-8"),
    )
    transcripts.transcribe("vid", 10, audio_dir, directory, ["python", "groq.py"], delete_audio=True)
    assert (directory / "vid.txt").read_text(encoding="utf-8").startswith("[00:00:01] Declaro aberta a sessão.")
    assert (directory / "vid.vtt").exists()
    assert not (audio_dir / "vid.mp3").exists()


def test_transcribe_failure_keeps_the_audio_and_leaves_no_partial_file(tmp_path, monkeypatch):
    (tmp_path / "vid.mp3").write_bytes(b"mp3")
    monkeypatch.setattr(transcripts, "transcribe_with_groq", lambda *arguments: None)
    with pytest.raises(RuntimeError, match="without writing the VTT"):
        transcripts.transcribe("vid", 10, tmp_path, tmp_path, ["python", "groq.py"], delete_audio=True)
    assert [path.name for path in tmp_path.iterdir()] == ["vid.mp3"]


def test_transcribe_with_groq_joins_chunks_with_offsets(tmp_path, monkeypatch, no_sleep):
    calls = []

    def fake_run(command, timeout):
        if command[0] == "ffmpeg":
            for number in range(2):
                transcripts.Path(command[-1].replace("%04d", f"{number:04d}")).write_bytes(b"chunk")
        else:
            calls.append(command)
            if len(calls) == 1:
                raise RuntimeError("rate limit")
            transcripts.Path(command[-1]).write_text(
                "WEBVTT\n\n1\n00:00:02.000 --> 00:00:03.000\nfala\n", encoding="utf-8"
            )

    monkeypatch.setattr(transcripts, "run_command", fake_run)
    target = tmp_path / "out.vtt"
    transcripts.transcribe_with_groq(tmp_path / "a.mp3", target, ["groq"], tmp_path, "vid")
    assert len(calls) == 3
    cues = transcripts.vtt_cues(target.read_text(encoding="utf-8"))
    assert cues == [(2.0, 3.0, "fala"), (1802.0, 1803.0, "fala")]
    assert [path.name for path in tmp_path.iterdir()] == ["out.vtt"]


def make_stage(name, done, log, fail=()):
    def work(video_id, progress):
        if video_id in fail:
            raise RuntimeError("broken")
        log.append((name, video_id))
        done.add((name, video_id))

    return work


def test_stages_hand_each_video_to_the_next_and_skip_finished_work(capsys):
    done = {("first", "b")}
    log = []
    stages = [
        transcripts.Stage(
            "first", 0, done=lambda v: ("first", v) in done, ready=lambda v: True, work=make_stage("first", done, log)
        ),
        transcripts.Stage(
            "second",
            1,
            done=lambda v: ("second", v) in done,
            ready=lambda v: ("first", v) in done,
            work=make_stage("second", done, log),
        ),
    ]
    transcripts.run_stages(stages, ["a", "b", "c"], {})
    assert [entry for entry in log if entry[0] == "first"] == [("first", "a"), ("first", "c")]
    assert [entry for entry in log if entry[0] == "second"] == [("second", "a"), ("second", "b"), ("second", "c")]
    assert (stages[0].finished, stages[0].skipped, stages[0].failures) == (2, 1, 0)
    assert "second: 3 done, 0 already there, 0 with error" in capsys.readouterr().err


def test_stage_stops_working_after_failures_in_a_row_and_next_stage_skips_them():
    done = set()
    log = []
    ids = ["a", "b", "c", "d", "e"]
    stages = [
        transcripts.Stage(
            "first",
            0,
            done=lambda v: ("first", v) in done,
            ready=lambda v: True,
            work=make_stage("first", done, log, fail={"b", "c", "d"}),
        ),
        transcripts.Stage(
            "second",
            1,
            done=lambda v: ("second", v) in done,
            ready=lambda v: ("first", v) in done,
            work=make_stage("second", done, log),
        ),
    ]
    transcripts.run_stages(stages, ids, {})
    assert (stages[0].finished, stages[0].failures) == (1, 3)
    assert [entry for entry in log if entry[0] == "first"] == [("first", "a")]
    assert [entry for entry in log if entry[0] == "second"] == [("second", "a")]


def test_run_stages_stops_the_threads_on_ctrl_c(monkeypatch):
    started = threading.Event()

    def slow(video_id, progress):
        started.set()
        threading.Event().wait(0.2)

    def interrupt(seconds):
        started.wait(2)
        raise KeyboardInterrupt

    stage = transcripts.Stage("slow", 0, done=lambda v: False, ready=lambda v: True, work=slow)
    monkeypatch.setattr(transcripts.time, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        transcripts.run_stages([stage], ["a", "b", "c", "d"], {})
    assert stage.finished < 4


def test_capture_needs_a_model_for_the_summary_step(data_dir, tmp_path, capsys):
    csv_path = tmp_path / "videos.csv"
    csv_path.write_text("url\nhttps://youtu.be/aaa\n", encoding="utf-8")
    assert transcripts.main(["capture", "--steps", "summary", str(csv_path)]) == 1
    assert "--model is required" in capsys.readouterr().err


def test_capture_runs_only_the_asked_steps_and_resumes(data_dir, tmp_path, monkeypatch):
    csv_path = tmp_path / "videos.csv"
    csv_path.write_text("url\nhttps://youtu.be/aaa\nhttps://youtu.be/bbb\n", encoding="utf-8")
    write_jsonl(data_dir / "videos.jsonl", [youtube_record("aaa", duration=60), youtube_record("bbb", duration=60)])
    (data_dir / "transcripts").mkdir()
    (data_dir / "transcripts" / f"{doc('aaa')}.txt").write_text("[00:00:01] já transcrito\n", encoding="utf-8")
    downloads = []

    def fake_download(document, url, directory, extra_args):
        downloads.append((url, extra_args))
        (directory / f"{document}.mp3").write_bytes(b"mp3")

    monkeypatch.setattr(transcripts, "download_audio", fake_download)
    arguments = ["capture", "--steps", "audio", "--yt-dlp-args", "--sleep-interval 3", str(csv_path)]
    assert transcripts.main(arguments) == 0
    # "aaa" already has a transcript, so its audio is not downloaded again.
    assert downloads == [("https://youtube.com/watch?v=bbb", ["--sleep-interval", "3"])]
    assert (data_dir / "audio" / f"{doc('bbb')}.mp3").exists()
    assert transcripts.main(arguments) == 0
    assert len(downloads) == 1


# --- site ---------------------------------------------------------------------------------------------------------


def test_read_transcript_ignores_lines_outside_the_format(tmp_path):
    path = tmp_path / "t.txt"
    path.write_text("[00:00:05] primeira\nsem marca\n[xx] ruim\n[01:00:00]   \n[01:00:01] segunda\n", encoding="utf-8")
    assert transcripts.read_transcript(path) == [[5, "primeira"], [3601, "segunda"]]


def test_display_model_name_hides_the_backend_tag():
    assert transcripts.display_model_name("s5-nano:4b-q8-256k") == "S5-nano"
    assert transcripts.display_model_name("S5-nano") == "S5-nano"
    assert transcripts.display_model_name("outro-modelo:latest") == "outro-modelo"


def test_read_houses_blanks_the_state_of_federal_houses(tmp_path):
    path = tmp_path / "videos.csv"
    path.write_text(
        "url,esfera,uf,orgao\nhttps://youtu.be/aaa,federal,DF,Câmara\nhttps://youtu.be/bbb,estadual,CE,Alece\n",
        encoding="utf-8",
    )
    assert transcripts.read_houses(path) == {
        doc("aaa"): {"house": "Câmara", "state": ""},
        doc("bbb"): {"house": "Alece", "state": "CE"},
    }
    assert transcripts.read_houses(None) == {}


def test_slim_list_replaces_categorical_values_with_positions():
    sessions = [
        {"id": "a", "title": "A", "house": "Senado", "state": "", "date": "2025-01-02", "duration": 60, "type": "Outro",
         "themes": ["Saúde", "Economia"], "summary": "ra"},
        {"id": "b", "title": "B", "house": "Alece", "state": "CE", "date": "2025-01-01", "duration": 30, "type": "Outro",
         "themes": ["Economia"], "summary": "rb"},
    ]  # fmt: skip
    slim = transcripts.slim_list(sessions, {"x": 1})
    assert slim["houses"] == ["Alece", "Senado"]
    assert slim["themes"] == ["Economia", "Saúde"]
    assert slim["fields"] == ["id", "title", "house", "state", "date", "duration", "type", "themes", "summary"]
    assert slim["items"][0] == ["a", "A", 1, 0, "2025-01-02", 60, 0, [1, 0], "ra"]
    assert slim["items"][1] == ["b", "B", 0, 1, "2025-01-01", 30, 0, [0], "rb"]
    assert slim["config"] == {"x": 1}


def test_minify_css_keeps_selectors_and_values():
    css = "/* comment */\n.a  .b {\n  color : red ;\n  margin: 0 auto;\n}\n\n.c:hover { top: calc(1px + 2px); }\n"
    assert transcripts.minify_css(css) == ".a .b{color : red;margin: 0 auto;}.c:hover{top: calc(1px + 2px);}"


def test_join_css_inlines_imports_and_removes_the_other_files(tmp_path):
    (tmp_path / "components").mkdir()
    (tmp_path / "tokens.css").write_text(":root { --x: 1; }\n", encoding="utf-8")
    (tmp_path / "components" / "card.css").write_text("/* card */\n.card { color: var(--x); }\n", encoding="utf-8")
    (tmp_path / "app.css").write_text(
        '/* layers */\n@import url("tokens.css");\n@import url("components/card.css");\n', encoding="utf-8"
    )
    transcripts.join_css(tmp_path)
    assert [path.name for path in tmp_path.iterdir()] == ["app.css"]
    assert (tmp_path / "app.css").read_text(encoding="utf-8") == ":root{--x: 1;}.card{color: var(--x);}\n"


@pytest.fixture
def captured_data(data_dir):
    """Three videos in the metadata: two complete, one without a summary."""
    write_jsonl(
        data_dir / "videos.jsonl",
        [
            youtube_record("old", title="Sessão antiga", channel_title="TV Câmara", duration=3600,
                           published_at="2025-01-10T10:00:00"),
            youtube_record("new", title="Sessão nova", channel_title="TV Alece", duration=1800,
                           started_at="2025-03-05T14:00:00", published_at="2025-03-06T00:00:00"),
            youtube_record("half", title="Incompleta", channel_title="TV", duration=60, published_at="2025-02-01"),
        ],
    )  # fmt: skip
    (data_dir / "transcripts").mkdir()
    (data_dir / "summaries").mkdir()
    for video_id in ("old", "new", "half"):
        (data_dir / "transcripts" / f"{doc(video_id)}.vtt").write_text(VTT, encoding="utf-8")
        (data_dir / "transcripts" / f"{doc(video_id)}.txt").write_text(
            f"[00:00:01] fala de {video_id} sobre orçamento\n[00:10:00] segunda fala\n", encoding="utf-8"
        )
    for video_id in ("old", "new"):
        analysis = {**VALID_ANALYSIS, "_meta": {"modelo": "s5-nano:4b-q8-256k"}}
        (data_dir / "summaries" / f"{doc(video_id)}.json").write_text(json.dumps(analysis), encoding="utf-8")
    return data_dir


def test_site_builds_the_data_files_for_complete_sessions(captured_data, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(transcripts, "fetch_thumbnail", lambda document, video_id, directory: video_id != "old")
    csv_path = tmp_path / "videos.csv"
    csv_path.write_text("url,esfera,uf,orgao\nhttps://youtu.be/new,estadual,CE,Alece\n", encoding="utf-8")
    output = tmp_path / "docs"
    (output / "data" / "items").mkdir(parents=True)
    (output / "data" / "items" / "stale.json").write_text("{}", encoding="utf-8")
    (output / "captions").mkdir(parents=True)
    (output / "captions" / "stale.vtt").write_text("", encoding="utf-8")
    (output / "js" / "modules").mkdir(parents=True)
    (output / "js" / "modules" / "renamed.js").write_text("", encoding="utf-8")

    assert transcripts.main(["site", "--csv", str(csv_path), "--output", str(output)]) == 0

    items = json.loads((output / "data" / "items.json").read_text(encoding="utf-8"))
    assert [row[0] for row in items["items"]] == [doc("new"), doc("old")]
    assert items["houses"] == ["Alece", "TV Câmara"]
    assert items["config"]["models"] == ["S5-nano"]
    assert items["config"]["thumbnail_fallback"] == {doc("old"): "https://i.ytimg.com/vi/old/mqdefault.jpg"}
    assert items["config"]["search_bytes"] == (output / "data" / "search.json").stat().st_size
    search = json.loads((output / "data" / "search.json").read_text(encoding="utf-8"))
    assert search["lines"][doc("new")] == [[1, "fala de new sobre orçamento"], [600, "segunda fala"]]
    assert search["keywords"] == {doc("new"): ["pl 123"], doc("old"): ["pl 123"]}
    session = json.loads((output / "data" / "items" / f"{doc('new')}.json").read_text(encoding="utf-8"))
    assert (session["id"], session["url"]) == (doc("new"), "https://youtube.com/watch?v=new")
    assert (session["house"], session["state"], session["date"], session["model"]) == (
        "Alece",
        "CE",
        "2025-03-05",
        "S5-nano",
    )
    assert session["participants"] == [
        {"name": person["nome"], "role": person.get("papel", "")} for person in VALID_ANALYSIS["participantes"]
    ]
    assert session["lines"] == search["lines"][doc("new")]
    assert sorted(path.name for path in (output / "data" / "items").iterdir()) == sorted(
        [f"{doc('new')}.json", f"{doc('old')}.json"]
    )
    assert not (output / "js" / "modules" / "renamed.js").exists()
    assert (output / "js" / "app.js").exists()
    assert sorted(path.name for path in (output / "captions").iterdir()) == sorted(
        [f"{doc('new')}.vtt", f"{doc('old')}.vtt"]
    )
    assert sorted(path.name for path in (output / "css").iterdir()) == ["app.css"]
    assert "@import" not in (output / "css" / "app.css").read_text(encoding="utf-8")
    assert (output / "CNAME").read_text(encoding="utf-8") == "transcricoes.brasil.io\n"
    assert (output / "index.html").exists()
    assert "1 videos left out, pipeline incomplete: https://youtube.com/watch?v=half" in capsys.readouterr().err


def test_site_can_exclude_videos_and_skip_the_cname(captured_data, tmp_path, monkeypatch):
    monkeypatch.setattr(transcripts, "fetch_thumbnail", lambda document, video_id, directory: True)
    output = tmp_path / "docs"
    excluded = f"old, {doc('half')}"
    assert transcripts.main(["site", "--exclude", excluded, "--domain", "", "--output", str(output)]) == 0
    items = json.loads((output / "data" / "items.json").read_text(encoding="utf-8"))
    assert [row[0] for row in items["items"]] == [doc("new")]
    assert not (output / "CNAME").exists()


def test_site_without_complete_videos_is_an_error(data_dir, tmp_path, capsys):
    write_jsonl(data_dir / "videos.jsonl", [youtube_record("half", title="x")])
    assert transcripts.main(["site", "--output", str(tmp_path / "docs")]) == 1
    assert "no video with the complete pipeline" in capsys.readouterr().err


def test_fetch_thumbnail_keeps_an_existing_webp_without_network(tmp_path, monkeypatch):
    (tmp_path / "doc.webp").write_bytes(b"webp")
    monkeypatch.setattr(transcripts.urllib.request, "urlopen", lambda *arguments, **options: pytest.fail("network"))
    assert transcripts.fetch_thumbnail("doc", "vid", tmp_path) is True


def test_fetch_thumbnail_reports_a_failed_download(tmp_path, monkeypatch, capsys):
    def offline(*arguments, **options):
        raise transcripts.urllib.error.URLError("offline")

    monkeypatch.setattr(transcripts.urllib.request, "urlopen", offline)
    assert transcripts.fetch_thumbnail("doc", "vid", tmp_path) is False
    assert list(tmp_path.iterdir()) == []
    assert "thumbnail of vid not downloaded" in capsys.readouterr().err


# --- measure and compare ------------------------------------------------------------------------------------------


def measurement(seconds, transferred, cards=20):
    step = {
        "step": "home",
        "seconds": seconds,
        "requests": 3,
        "transferred": transferred,
        "decompressed": transferred * 3,
        "js_memory": 2 * 2**20,
        "dom_nodes": 100,
        "files": [],
        "transferred_total": transferred,
        "state": {"cards": cards},
    }
    return {"profiles": {"desktop": {"1. Home only": {"steps": [step], "blocked": 0}}}}


def test_format_size():
    assert transcripts.format_size(1536) == "2 KB"
    assert transcripts.format_size(3 * 2**20) == "3.00 MB"


def test_comparison_shows_both_measurements_side_by_side():
    lines = transcripts.comparison_lines(measurement(1.5, 400_000), measurement(0.5, 2 * 2**20))
    assert "## desktop" in lines
    assert "| 1. Home only | home | 1.50 s | 0.50 s | 391 KB | 2.00 MB | 2.00 MB | 2.00 MB |" in lines
    assert lines[-1] == "Steps where the screen showed something different: 0"


def test_comparison_lists_steps_where_the_screen_differs_and_new_scenarios():
    after = measurement(0.5, 1000, cards=311)
    after["profiles"]["desktop"]["2. New"] = after["profiles"]["desktop"]["1. Home only"]
    lines = transcripts.comparison_lines(measurement(1.5, 1000), after)
    assert "Steps where the screen showed something different: 1" in lines
    assert lines[-1] == "  [desktop] 1. Home only | home"
    assert any(line.startswith("only in the second [desktop] 2. New | home") for line in lines)


def test_compare_command_prints_the_tables(tmp_path, capsys):
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    before.write_text(json.dumps(measurement(1.5, 1000)), encoding="utf-8")
    after.write_text(json.dumps(measurement(0.5, 1000)), encoding="utf-8")
    assert transcripts.main(["compare", str(before), str(after)]) == 0
    assert "| 1. Home only | **total** | **1.50 s** | **0.50 s**" in capsys.readouterr().out


def test_median_session_picks_a_typical_one(tmp_path):
    items = tmp_path / "data" / "items"
    items.mkdir(parents=True)
    for name, size in (("small", 10), ("medium", 100), ("large", 1000)):
        (items / f"{name}.json").write_text("x" * size, encoding="utf-8")
    assert transcripts.median_session(tmp_path) == "medium"
    with pytest.raises(transcripts.CommandError, match="is empty"):
        transcripts.median_session(tmp_path / "nothing")


def test_measure_without_a_browser_is_an_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(transcripts, "find_browser", lambda: "")
    assert transcripts.main(["measure", str(tmp_path), str(tmp_path / "out.json")]) == 1
    assert "no Chrome or Chromium found" in capsys.readouterr().err


# --- command line -------------------------------------------------------------------------------------------------


def test_every_command_has_help(capsys):
    for command in transcripts.COMMANDS:
        with pytest.raises(SystemExit) as exit_info:
            transcripts.main([command, "--help"])
        assert exit_info.value.code == 0
        assert f"usage: transcripts {command}" in capsys.readouterr().out


def test_a_command_is_required(capsys):
    with pytest.raises(SystemExit) as exit_info:
        transcripts.main([])
    assert exit_info.value.code == 2


@pytest.mark.parametrize(
    "arguments",
    [
        ["select", "--since", "2025-13-01", "videos.csv", "10"],
        ["capture", "--steps", "audio,unknown", "videos.csv"],
        ["capture", "--transcriber", "other", "videos.csv"],
        ["measure", "--profiles", "tablet", "docs", "out.json"],
        ["site", "--unknown-option"],
    ],
)
def test_invalid_arguments_exit_with_code_2(arguments, capsys):
    with pytest.raises(SystemExit) as exit_info:
        transcripts.main(arguments)
    assert exit_info.value.code == 2


def test_capture_arguments_have_the_expected_defaults():
    args = transcripts.create_parser().parse_args(["capture", "videos.csv"])
    assert args.steps == ["metadata", "audio", "transcript", "summary"]
    assert (args.transcriber, args.backend, args.delete_audio) == ("digue", "ollama", False)
    assert (args.max_words, args.overlap_lines) == (8000, 10)
    assert args.groq_command.endswith("groq.py")


def test_ctrl_c_exits_with_code_130(monkeypatch, capsys):
    def interrupted(args):
        raise KeyboardInterrupt

    monkeypatch.setitem(transcripts.COMMANDS, "compare", ("help", lambda parser: None, interrupted))
    assert transcripts.main(["compare"]) == 130
    assert "interrupted" in capsys.readouterr().err
