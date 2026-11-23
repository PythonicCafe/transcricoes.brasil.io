# transcricoes.brasil.io

First public version of open, searchable transcripts of Brazil's public legislative sessions (Chamber of Deputies, Senate and state
assemblies), by [Pythonic Café](https://pythonic.cafe/) and [Brasil.IO](https://brasil.io/).

Site: <https://transcricoes.brasil.io/>

Everything here runs with open models: Whisper for transcription and S5-nano (4 billion parameters, by Maritaca AI)
for summaries and themes. Each video is processed once, in batch; searching uses no AI. Transcripts and summaries
are machine-generated, were not reviewed by people and contain errors.

## What is in this repository

| File | What it does |
|---|---|
| `pipeline.py` | For each video in a CSV: metadata (YouTube API through [youtool](https://github.com/PythonicCafe/youtool)), audio download (`yt-dlp`), transcription (local, through `digue`/whisper.cpp, or `groq.py`) and summary (S5-nano through ollama or Maritaca's API). Resumable: nothing is done twice. |
| `groq.py` | Transcribes audio with Whisper on Groq's API, with voice activity detection. |
| `gera_site.py` | Builds the static site in `docs/` from the pipeline output. Only videos with the full pipeline done are included. Each session also gets its captions as a WebVTT file for download. |
| `site/` | The site itself: HTML, CSS with design tokens and plain JavaScript modules, no build step. Search runs in the browser over all transcripts. |
| `canais_horas.py` | Measures how many hours of video each legislative YouTube channel has (`canais.csv`). |
| `testa_resumo.py` | Runs the same summary prompt on other models, to compare results. |
| `videos.csv` | The videos chosen for this first version. |
| `mede_site.py`, `compara.py` | Measure the generated site in a real browser (time, network, memory) and compare two measurements. |

## Running

```sh
pip install -r requirements.txt   # also needs ffmpeg
export YOUTUBE_API_KEY=...        # secrets only through environment variables

# local models: digue (whisper.cpp) and ollama
python pipeline.py --modelo s5-nano:4b-q8-256k videos.csv

# same open models on rented inference, to go faster
export GROQ_API_KEY=... MARITACA_API_KEY=...
python pipeline.py --transcritor groq --backend maritaca --modelo S5-nano videos.csv

python gera_site.py --csv videos.csv   # writes docs/
```

This version searches in the browser so it can be a static site. The production version will use PostgreSQL full-text
search on Brasil.IO.

## Development

Everything runs inside a development container (Python dependencies, ffmpeg, Chromium and Deno), so the code and its
dependencies do not run on your machine. The repository is mounted, not copied, and the pipeline data lives in
`dados/`, which git ignores. `make` alone lists every command:

```sh
make build              # build the image, once
make select HOURS=100   # add new sessions to videos.csv (make select-dry-run only shows them)
make capture            # download, transcribe and summarize what is missing
make site               # write docs/
make serve              # check it at http://localhost:8000
make publish            # commit docs/ and videos.csv, then push
make measure            # measure the site in Chromium, without network
```

`make capture` reads `GROQ_API_KEY`, `MARITACA_API_KEY` and `YOUTUBE_API_KEY` from the environment. By default the
container cannot see your browser; `make capture COOKIES=1` mounts the Firefox profile read-only, for when YouTube
asks for a login. `python compara.py before.json after.json` compares two measurements.

In `site/` the CSS is split by layer and component; `gera_site.py` joins it into a single `docs/css/app.css`.

## License

Copyright (c) 2026 Pythonic Café.

GNU Lesser General Public License, version 3 or later (LGPL-3.0-or-later). See `LICENSE` (LGPL) and `LICENSE.GPL`
(the GPL text it builds on). Bootstrap, in `site/vendor/`, is distributed under its own MIT license.
