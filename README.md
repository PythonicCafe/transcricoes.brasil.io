# transcricoes.brasil.io

Primeira versão pública de transcrições abertas e pesquisáveis das sessões públicas do Legislativo brasileiro (Câmara
dos Deputados, Senado Federal e assembleias estaduais), feita pela [Pythonic Café](https://pythonic.cafe/) e pelo
[Brasil.IO](https://brasil.io/).

Site: <https://transcricoes.brasil.io/>

Tudo aqui roda com modelos abertos: Whisper para a transcrição e S5-nano (4 bilhões de parâmetros, da Maritaca AI)
para resumos e temas. Cada vídeo é processado uma vez, em lote; a busca não usa IA. Transcrições e resumos são
gerados por máquina, não foram revisados por pessoas e contêm erros.

## O que há neste repositório

| Arquivo | O que faz |
|---|---|
| `transcripts.py` | A ferramenta de linha de comando, com um subcomando por etapa do trabalho (veja abaixo). |
| `groq.py` | Transcreve áudio com o Whisper pela API da Groq, com detecção de voz. |
| `site/` | O site: HTML, CSS com _design tokens_ e módulos de JavaScript puro, sem etapa de _build_. A busca roda no navegador, sobre todas as transcrições. |
| `docs/` | O site gerado, que o GitHub Pages publica. |
| `videos.csv` | Os vídeos escolhidos para esta versão. |
| `canais.csv` | Os canais do YouTube de cada casa legislativa. |
| `tests/` | Testes da linha de comando (Python) e dos módulos do site (`tests/js/`, JavaScript). |
| `testa_resumo.py` | Refaz resumos em outros modelos, para comparar resultados. |
| `PUBLICAR.md` | Passo a passo da publicação no GitHub Pages. |

## Subcomandos

`python transcripts.py --help` lista os subcomandos, e cada um tem o seu `--help`.

| Subcomando | O que faz |
|---|---|
| `channels` | Conta as horas de vídeo no canal de cada casa legislativa, pela API do YouTube (por meio da [youtool](https://github.com/PythonicCafe/youtool)). |
| `select` | Escolhe mais sessões plenárias para a captura, em rodízio entre as casas, sem usar a API. |
| `capture` | Para cada vídeo de um CSV: metadados, áudio (`yt-dlp`), transcrição (local, com `digue`/whisper.cpp, ou pelo `groq.py`) e resumo (S5-nano no ollama ou na API da Maritaca). Retoma de onde parou: nada é feito duas vezes. |
| `site` | Monta o site estático em `docs/` a partir do que foi capturado. Só entram vídeos com todas as etapas concluídas. |
| `measure` | Mede o site gerado num navegador de verdade (tempo, rede e memória). |
| `compare` | Compara duas medições. |

Os dados da captura ficam em `data/`, que o git ignora. O código, a linha de comando, os arquivos do site e as chaves
dos JSON do site estão em inglês. A exceção são os resumos em `data/summaries/`: as chaves seguem o esquema em
português pedido ao modelo (`resumo`, `temas`, `palavras_chave`...), e o subcomando `site` as converte.

Cada documento (hoje, cada vídeo) tem um ID próprio, que não depende do YouTube: o UUIDv5 (namespace URL) da sua URL
canônica, como no DocSearch. Para vídeos do YouTube a URL canônica é `https://youtube.com/watch?v=<id>`. Os arquivos
em `data/` e em `docs/` têm esse ID no nome, e cada linha do `data/videos.jsonl` traz o `uuid` e a `url`.

As rotas do site ficam no fragmento da URL (`#/v/<id>/<segundos>` para uma sessão). A página "Como foi feito" tem um
endereço por idioma (`#/about`, `#/sobre`, `#/acerca-de`); qualquer um deles abre a página, que passa a mostrar o do
idioma escolhido.

## Como usar

Tudo roda dentro de um contêiner de desenvolvimento (dependências Python, ffmpeg, Chromium e Deno), então o código e
as dependências não rodam direto na sua máquina. O repositório é montado no contêiner, não copiado. O `make` sozinho
lista todos os comandos:

```sh
make build              # constrói a imagem, uma vez
make select HOURS=100   # acrescenta sessões novas ao videos.csv (make select-dry-run só mostra)
make capture            # baixa, transcreve e resume o que falta
make site               # gera docs/
make serve              # para conferir em http://localhost:8000
make publish            # faz o commit de docs/ e do videos.csv e envia para o GitHub
make test               # roda os testes (make test-py e make test-js rodam cada parte)
make lint               # formata e verifica o código
make measure            # mede o site no Chromium, sem rede
```

O `make capture` lê `GROQ_API_KEY`, `MARITACA_API_KEY` e `YOUTUBE_API_KEY` do ambiente; as chaves nunca ficam em
arquivo. Por padrão o contêiner não enxerga o seu navegador. O `make capture COOKIES=1` monta o perfil do Firefox só
para leitura, para quando o YouTube pedir login.

Valores que mudam de máquina para máquina podem ir num `config.mk`, que o git ignora. As dependências estão em grupos
no `pyproject.toml`.

Em `site/` o CSS é dividido por camada e por componente; o subcomando `site` junta tudo num só `docs/css/app.css`.
Esta versão busca no navegador para poder ser um site estático. A versão de produção vai usar a busca textual do
PostgreSQL no Brasil.IO.

## Licença

Copyright (c) 2026 Pythonic Café.

GNU Lesser General Public License, versão 3 ou posterior (LGPL-3.0-or-later). Veja `LICENSE` (LGPL) e `LICENSE.GPL`
(o texto da GPL em que ela se apoia). O Bootstrap, em `site/vendor/`, é distribuído sob a sua própria licença MIT.
