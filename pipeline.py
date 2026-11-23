"""Pipeline: metadados (youtool) -> áudio (yt-dlp) -> transcrição (digue) -> resumo e temas (S5-nano).

Lê um CSV com uma coluna `url` (ou `URL`; se não houver, usa a primeira coluna) e, para cada vídeo:

1. metadados: API do YouTube via youtool, gravados em JSONL append-only (`dados/videos.jsonl`), chave = id do vídeo;
2. audio: `yt-dlp` extrai só o áudio em MP3 (`dados/audio/<id>.mp3`);
3. transcricao: `digue transcribe -f vtt` gera `dados/transcricao/<id>.vtt` e `digue convert` gera `<id>.txt`;
   com `--transcritor groq`, o VTT vem de um script externo que chama a API da Groq (`--groq-comando`) e o `.txt`
   é gerado aqui mesmo, no formato `[HH:MM:SS] texto`;
4. resumo: LLM (ollama local ou API da Maritaca) gera `dados/resumo/<id>.json` com resumo, temas etc.

As etapas 2, 3 e 4 rodam em paralelo entre si, cada uma em sua thread e processando UM vídeo por vez: enquanto um
áudio baixa, outro é transcrito e um terceiro é resumido. Nunca há dois downloads, duas transcrições ou duas chamadas
ao LLM ao mesmo tempo. Um vídeo passa para a etapa seguinte assim que a anterior termina.

Nenhuma etapa refaz trabalho: cada uma pula o vídeo cujo arquivo final já existe. Todo arquivo é escrito com nome
temporário e renomeado só ao terminar, então uma interrupção nunca deixa arquivo final pela metade.

Segredos só por variável de ambiente: `YOUTUBE_API_KEY` (uma ou mais chaves separadas por vírgula) e
`MARITACA_API_KEY`. O script da Groq herda o ambiente deste processo (inclusive a chave dele).

Transcrições longas usam a técnica "resumo das partes com contexto" (do notebook "Investigando financiadores de
desinformação com Python, IA e postgres"): o texto é dividido em partes de até `--max-palavras`, com sobreposição de
`--linhas-sobrepostas` linhas; cada parte é resumida em tópicos recebendo os tópicos de todas as partes anteriores
como contexto (a resposta "NULL" descarta partes sem conteúdo relevante); no fim, uma chamada gera o JSON final a
partir dos tópicos. Transcrições que cabem em uma parte vão direto para o JSON final, em uma chamada só. Sem
tokenizador local do modelo, o tamanho é estimado em palavras (~1,5 token por palavra em português).
"""

import argparse
import csv
import itertools
import json
import os
import queue
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from tqdm import tqdm

# Os dados ficam sempre em `dados/`, ao lado dos scripts (o git ignora a pasta).
PASTA_DADOS = Path(__file__).resolve().parent / "dados"
ETAPAS = ["metadados", "audio", "transcricao", "resumo"]
MARITACA_URL = "https://chat.maritaca.ai/api"

# Temas oficiais da Câmara dos Deputados para classificar proposições
# (https://dadosabertos.camara.leg.br/api/v2/referencias/proposicoes/codTema).
TEMAS = [
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

TIPOS_SESSAO = [
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
        "tipo_sessao": {"type": "string", "enum": TIPOS_SESSAO},
        "temas": {"type": "array", "items": {"type": "string", "enum": TEMAS}, "minItems": 1, "maxItems": 3},
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

INSTRUCOES = f"""Você analisa transcrições automáticas de vídeos de sessões públicas do Legislativo brasileiro \
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
- "tipo_sessao": string. Uma destas opções: {"; ".join(TIPOS_SESSAO)}.
- "temas": lista de 1 a 3 strings, escolhidas SOMENTE desta lista (copie o texto exato), do mais para o menos \
central: {"; ".join(TEMAS)}.
- "palavras_chave": lista de 3 a 8 strings com termos específicos da sessão (projetos com número, nomes de \
programas, empresas, lugares, eventos), em minúsculas, sem repetir os temas.
- "participantes": lista de até 12 objetos {{"nome": string, "papel": string}} com quem conduziu ou falou \
(ex.: papel "presidente da comissão", "deputado federal PT-SP", "convidado, diretor da empresa X").
- "decisoes": lista de strings com votações, aprovações, rejeições, requerimentos e encaminhamentos com resultado. \
Lista vazia se não houve."""

REGRAS_TOPICOS = """Regras:
- O resumo deve ser em tópicos, um por linha, começando com "- "
- Utilize APENAS informações que estão na transcrição{e_no_resumo}
- Responda APENAS com os tópicos
- Sua resposta deve ter NO MÁXIMO 8 tópicos concisos
- Preserve nomes de pessoas com cargo ou partido, números de projetos, valores, votações e decisões com resultado
- A transcrição é automática e não diz quem fala: não invente nomes nem atribua falas sem evidência no texto
- Seja neutro, sem adjetivos de opinião"""

PROMPT_PARTE_1 = """O conteúdo abaixo é a parte {parte} (de {total}) da transcrição automática de um vídeo de sessão \
pública do Legislativo brasileiro. Sua tarefa é resumi-la.

{contexto}

{regras}

Transcrição:
-----
{transcricao}
-----"""

PROMPT_PARTE_N = """O conteúdo abaixo é a parte {parte} (de {total}) da transcrição automática de um vídeo de sessão \
pública do Legislativo brasileiro. Sua tarefa é resumi-la.
Já foi feito um resumo das partes anteriores a essa, que também segue abaixo, para contextualização. Resuma APENAS \
a parte atual, sem repetir o que já está no resumo preliminar.

{contexto}

{regras}
- ATENÇÃO: se a transcrição não tiver pontos relevantes, sua resposta deve ser apenas "NULL" (sem aspas)

Resumo preliminar:
-----
{resumo}
-----

Transcrição:
-----
{transcricao}
-----"""

INSTRUCOES_FINAL_TOPICOS = """ATENÇÃO: em vez da transcrição, você recebeu os tópicos com os principais pontos da \
sessão, em ordem cronológica, extraídos de partes consecutivas da transcrição. Onde as regras e os campos dizem \
"transcrição", leia "tópicos". Utilize APENAS informações que estão nos tópicos e NÃO REMOVA conteúdo relevante."""


# --- utilidades ---------------------------------------------------------------------------------------------------


def id_do_video(url: str) -> str | None:
    """Extrai o id de URLs `watch?v=`, `youtu.be/`, `/live/`, `/shorts/` e `/embed/`."""
    partes = urllib.parse.urlparse(url.strip())
    if partes.netloc.endswith("youtu.be"):
        return partes.path.strip("/") or None
    consulta = urllib.parse.parse_qs(partes.query)
    if "v" in consulta:
        return consulta["v"][0]
    pedacos = [pedaco for pedaco in partes.path.split("/") if pedaco]
    if len(pedacos) >= 2 and pedacos[0] in ("live", "shorts", "embed"):
        return pedacos[1]
    return None


def le_csv(caminho: Path) -> list[str]:
    """Devolve os ids dos vídeos, na ordem do CSV e sem repetição."""
    with caminho.open(encoding="utf-8-sig", newline="") as arquivo:
        leitor = csv.reader(arquivo)
        cabecalho = next(leitor)
        nomes = [nome.strip().lower() for nome in cabecalho]
        coluna = nomes.index("url") if "url" in nomes else 0
        ids = []
        for linha in leitor:
            if len(linha) <= coluna or not linha[coluna].strip():
                continue
            video_id = id_do_video(linha[coluna])
            if video_id is None:
                tqdm.write(f"AVISO: URL sem id de vídeo, ignorada: {linha[coluna]}")
            elif video_id not in ids:
                ids.append(video_id)
    return ids


def le_jsonl(caminho: Path) -> dict[str, dict]:
    registros = {}
    if caminho.exists():
        with caminho.open(encoding="utf-8") as arquivo:
            for linha in arquivo:
                if linha.strip():
                    registro = json.loads(linha)
                    registros[registro["id"]] = registro
    return registros


def executa(comando: list[str], timeout: float) -> None:
    """Roda um subprocesso; em erro, levanta RuntimeError com o fim do stderr."""
    try:
        subprocess.run(comando, capture_output=True, check=True, timeout=timeout)
    except subprocess.CalledProcessError as erro:
        fim = (erro.stderr or b"").decode("utf-8", errors="replace").strip()[-600:]
        raise RuntimeError(f"{comando[0]} saiu com código {erro.returncode}: {fim}") from erro
    except subprocess.TimeoutExpired as erro:
        raise RuntimeError(f"{comando[0]} passou do timeout de {timeout:.0f} s") from erro


def remove_temporarios(pasta: Path, video_id: str) -> None:
    for temporario in pasta.glob(f"{video_id}.tmp*"):
        temporario.unlink()


def minutos(metadados: dict[str, dict], video_id: str) -> float:
    return (metadados.get(video_id, {}).get("duration") or 600) / 60


# --- etapa 1: metadados -------------------------------------------------------------------------------------------


def coleta_metadados(ids: list[str], caminho: Path) -> dict[str, dict]:
    existentes = le_jsonl(caminho)
    faltam = [video_id for video_id in ids if video_id not in existentes]
    if not faltam:
        tqdm.write(f"metadados: {len(ids)} vídeos já coletados")
        return existentes
    chaves = [chave.strip() for chave in os.environ.get("YOUTUBE_API_KEY", "").split(",") if chave.strip()]
    if not chaves:
        sys.exit(f"ERRO: defina YOUTUBE_API_KEY para coletar metadados (faltam {len(faltam)} vídeos)")
    from youtool import YouTube

    youtube = YouTube(chaves)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with (
        caminho.open("a", encoding="utf-8") as arquivo,
        tqdm(total=len(faltam), desc="metadados", unit="vídeo") as progresso,
    ):
        for registro in youtube.videos_infos(faltam):
            arquivo.write(json.dumps(registro, ensure_ascii=False, default=str) + "\n")
            arquivo.flush()
            existentes[registro["id"]] = json.loads(json.dumps(registro, default=str))
            progresso.update(1)
    for video_id in faltam:
        if video_id not in existentes:
            tqdm.write(f"AVISO: a API não devolveu o vídeo {video_id} (privado, removido ou id errado)")
    return existentes


# --- etapa 2: áudio -----------------------------------------------------------------------------------------------


def baixa_audio(video_id: str, pasta: Path, extras: list[str]) -> None:
    """Baixa só o áudio em MP3; levanta RuntimeError em caso de falha."""
    remove_temporarios(pasta, video_id)
    comando = [
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
        str(pasta / f"{video_id}.tmp.%(ext)s"),
        *extras,
        "--",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    try:
        executa(comando, timeout=3600)
        (pasta / f"{video_id}.tmp.mp3").rename(pasta / f"{video_id}.mp3")
    except (RuntimeError, OSError) as erro:
        remove_temporarios(pasta, video_id)
        raise RuntimeError(str(erro)) from erro


# --- etapa 3: transcrição -----------------------------------------------------------------------------------------


def vtt_para_texto(vtt: str) -> str:
    """Converte WebVTT em linhas `[HH:MM:SS] texto`, uma por legenda (mesmo formato do `digue convert`)."""
    linhas: list[str] = []
    inicio: str | None = None
    falas: list[str] = []
    for linha in [*vtt.splitlines(), ""]:
        linha = linha.strip()
        if "-->" in linha:
            inicio = linha.split("-->")[0].strip().split(".")[0]
            if inicio.count(":") == 1:
                inicio = f"00:{inicio}"
            falas = []
        elif not linha:
            if inicio and falas:
                linhas.append(f"[{inicio}] {' '.join(falas)}")
            inicio, falas = None, []
        elif inicio:
            falas.append(linha)
    return "\n".join(linhas) + "\n"


MINUTOS_POR_PEDACO = 30


def segundos_do_vtt(marca: str) -> float:
    partes = [float(parte) for parte in marca.strip().split(":")]
    while len(partes) < 3:
        partes.insert(0, 0.0)
    return partes[0] * 3600 + partes[1] * 60 + partes[2]


def marca_do_vtt(segundos: float) -> str:
    milissegundos = round(segundos * 1000)
    horas, resto = divmod(milissegundos, 3_600_000)
    minutos_, resto = divmod(resto, 60_000)
    return f"{horas:02d}:{minutos_:02d}:{resto // 1000:02d}.{resto % 1000:03d}"


def legendas_do_vtt(vtt: str, deslocamento: float) -> list[tuple[float, float, str]]:
    """Legendas (início, fim, texto) de um VTT, com os tempos somados a `deslocamento` segundos."""
    legendas: list[tuple[float, float, str]] = []
    tempos: tuple[float, float] | None = None
    falas: list[str] = []
    for linha in [*vtt.splitlines(), ""]:
        linha = linha.strip()
        if "-->" in linha:
            inicio, fim = linha.split("-->")[:2]
            tempos = (segundos_do_vtt(inicio) + deslocamento, segundos_do_vtt(fim.split()[0]) + deslocamento)
            falas = []
        elif not linha:
            if tempos and falas:
                legendas.append((*tempos, " ".join(falas)))
            tempos, falas = None, []
        elif tempos:
            falas.append(linha)
    return legendas


def transcreve_groq(audio: Path, destino: Path, comando_groq: list[str], pasta: Path, video_id: str) -> None:
    """Corta o áudio em pedaços mono de 16 kHz, transcreve um por vez e junta tudo em um VTT.

    O script da Groq decodifica o arquivo inteiro na memória; com sessões de várias horas o processo é morto pelo
    sistema (código -9). Em pedaços de 30 minutos cada chamada usa poucas dezenas de MB. Cada pedaço tem até 3
    tentativas, para sobreviver a limite de requisições da API.
    """
    with tempfile.TemporaryDirectory(dir=pasta, prefix=f"{video_id}.partes.") as nome_temporario:
        temporaria = Path(nome_temporario)
        executa(
            [
                "ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(audio), "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "libmp3lame", "-b:a", "48k", "-f", "segment", "-segment_time", str(MINUTOS_POR_PEDACO * 60),
                "-reset_timestamps", "1", str(temporaria / "%04d.mp3"),
            ],
            timeout=3600,
        )  # fmt: skip
        legendas: list[tuple[float, float, str]] = []
        pedacos = sorted(temporaria.glob("*.mp3"))
        if not pedacos:
            raise RuntimeError("o ffmpeg não gerou pedaços de áudio")
        for numero, pedaco in enumerate(pedacos):
            vtt_pedaco = pedaco.with_suffix(".vtt")
            for tentativa in range(1, 4):
                try:
                    executa([*comando_groq, str(pedaco), str(vtt_pedaco)], timeout=1200)
                    break
                except RuntimeError as erro:
                    if tentativa == 3:
                        raise RuntimeError(f"pedaço {numero + 1}/{len(pedacos)}: {erro}") from erro
                    time.sleep(30 * tentativa)
            if vtt_pedaco.exists():
                legendas += legendas_do_vtt(vtt_pedaco.read_text(encoding="utf-8"), numero * MINUTOS_POR_PEDACO * 60)
        linhas = ["WEBVTT", ""]
        for numero, (inicio, fim, texto) in enumerate(legendas, start=1):
            linhas += [str(numero), f"{marca_do_vtt(inicio)} --> {marca_do_vtt(fim)}", texto, ""]
        destino.write_text("\n".join(linhas), encoding="utf-8")


def transcreve(
    video_id: str, minutos_de_audio: float, pasta_audio: Path, pasta: Path, comando_groq: list[str] | None = None
) -> None:
    """Gera <id>.vtt e <id>.txt; levanta RuntimeError em caso de falha.

    Sem `comando_groq`, usa o digue (local). Com ele, roda `comando_groq + [audio, vtt]` (script que chama a API da
    Groq) e converte o VTT para texto aqui, sem depender do `digue convert`.
    """
    remove_temporarios(pasta, video_id)
    vtt = pasta / f"{video_id}.vtt"
    audio = pasta_audio / f"{video_id}.mp3"
    try:
        if not vtt.exists():
            vtt_temporario = pasta / f"{video_id}.tmp.vtt"
            # Folga generosa: 2x a duração do áudio, mínimo de 30 min.
            limite = max(1800, minutos_de_audio * 60 * 2)
            if comando_groq is None:
                executa(["digue", "transcribe", "-f", "vtt", str(audio), "-o", str(vtt_temporario)], limite)
            else:
                transcreve_groq(audio, vtt_temporario, comando_groq, pasta, video_id)
            if not vtt_temporario.exists() or not vtt_temporario.stat().st_size:
                raise RuntimeError("o transcritor terminou sem gerar o VTT")
            vtt_temporario.rename(vtt)
        texto_temporario = pasta / f"{video_id}.tmp.txt"
        if comando_groq is None:
            executa(["digue", "convert", str(vtt), str(texto_temporario)], timeout=300)
        else:
            texto = vtt_para_texto(vtt.read_text(encoding="utf-8"))
            if not texto.strip():
                raise RuntimeError("VTT sem legendas")
            texto_temporario.write_text(texto, encoding="utf-8")
        texto_temporario.rename(pasta / f"{video_id}.txt")
    except (RuntimeError, OSError) as erro:
        remove_temporarios(pasta, video_id)
        raise RuntimeError(str(erro)) from erro


def transcreve_e_limpa(
    video_id: str,
    minutos_de_audio: float,
    pasta_audio: Path,
    pasta: Path,
    comando_groq: list[str] | None,
    apaga_audio: bool,
) -> None:
    """Transcreve e, se pedido, apaga o MP3 (só depois de o .txt existir, para uma falha não custar novo download)."""
    transcreve(video_id, minutos_de_audio, pasta_audio, pasta, comando_groq)
    if apaga_audio and (pasta / f"{video_id}.txt").exists():
        (pasta_audio / f"{video_id}.mp3").unlink(missing_ok=True)


# --- etapa 4: resumo ----------------------------------------------------------------------------------------------


class ErroLLM(Exception):
    pass


def post_json(url: str, corpo: dict, cabecalhos: dict[str, str], timeout: float) -> dict:
    requisicao = urllib.request.Request(
        url, data=json.dumps(corpo).encode("utf-8"), headers={"Content-Type": "application/json", **cabecalhos}
    )
    try:
        with urllib.request.urlopen(requisicao, timeout=timeout) as resposta:
            return json.loads(resposta.read())
    except urllib.error.HTTPError as erro:
        detalhe = erro.read().decode("utf-8", errors="replace")[:400]
        raise ErroLLM(f"HTTP {erro.code} em {url}: {detalhe}") from erro
    except (urllib.error.URLError, TimeoutError) as erro:
        raise ErroLLM(f"falha de rede em {url}: {erro}") from erro


class GeradorOllama:
    """API nativa do ollama; `format` com JSON schema restringe a saída."""

    def __init__(self, url: str, modelo: str, timeout: float, temperatura: float) -> None:
        self.url = url.rstrip("/") + "/api/chat"
        self.modelo = modelo
        self.timeout = timeout
        self.temperatura = temperatura

    def gera(self, mensagens: list[dict], estruturado: bool) -> str:
        corpo = {
            "model": self.modelo,
            "messages": mensagens,
            "stream": False,
            "think": False,
            "options": {"num_ctx": 32768, "temperature": self.temperatura, "top_p": 0.8, "top_k": 20},
        }
        if estruturado:
            corpo["format"] = SCHEMA
        return post_json(self.url, corpo, {}, self.timeout)["message"]["content"]


class GeradorMaritaca:
    """API da Maritaca, compatível com a da OpenAI (`/chat/completions`). O formato vem só pelo prompt."""

    def __init__(self, url: str, modelo: str, timeout: float, temperatura: float) -> None:
        chave = os.environ.get("MARITACA_API_KEY", "").strip()
        if not chave:
            sys.exit("ERRO: defina MARITACA_API_KEY para usar --backend maritaca")
        self.url = url.rstrip("/") + "/chat/completions"
        self.modelo = modelo
        self.timeout = timeout
        self.temperatura = temperatura
        self.cabecalhos = {"Authorization": f"Bearer {chave}"}
        self.pede_pouco_raciocinio = True

    def gera(self, mensagens: list[dict], estruturado: bool) -> str:
        # A família S5 só aceita os valores padrão de `temperature` e `top_p`, então não são enviados. O limite é
        # alto porque o raciocínio do modelo, quando existe, consome tokens de saída antes da resposta.
        corpo = {"model": self.modelo, "messages": mensagens, "max_tokens": 16000}
        if self.pede_pouco_raciocinio:
            # Sem isso o modelo às vezes gasta os 16 mil tokens só raciocinando e devolve conteúdo vazio. Se a API
            # recusar o parâmetro, ele deixa de ser enviado nas próximas chamadas.
            try:
                resposta = post_json(self.url, {**corpo, "reasoning_effort": "low"}, self.cabecalhos, self.timeout)
            except ErroLLM as erro:
                if "HTTP 400" not in str(erro) or "reasoning" not in str(erro):
                    raise
                tqdm.write("  a API não aceita reasoning_effort; seguindo sem ele")
                self.pede_pouco_raciocinio = False
                resposta = post_json(self.url, corpo, self.cabecalhos, self.timeout)
        else:
            resposta = post_json(self.url, corpo, self.cabecalhos, self.timeout)
        escolha = resposta["choices"][0]
        conteudo = escolha["message"].get("content")
        if not conteudo:
            raise ErroLLM(
                f"resposta sem conteúdo: finish_reason={escolha.get('finish_reason')}, "
                f"campos da mensagem={sorted(escolha['message'])}, usage={resposta.get('usage')}"
            )
        return conteudo


def extrai_json(texto: str) -> dict:
    """Aceita JSON puro, dentro de bloco ```json ou depois de um bloco <think>."""
    texto = texto.split("</think>")[-1]
    inicio, fim = texto.find("{"), texto.rfind("}")
    if inicio == -1 or fim <= inicio:
        raise ValueError("resposta sem objeto JSON")
    return json.loads(texto[inicio : fim + 1])


def valida(analise: dict) -> dict:
    """Confere campos e tipos e descarta temas fora da lista oficial; levanta ValueError se não sobrar tema."""
    for campo in SCHEMA["required"]:
        if campo not in analise:
            raise ValueError(f"campo ausente: {campo}")
    if not isinstance(analise["resumo"], str) or not analise["resumo"].strip():
        raise ValueError("resumo vazio")
    for campo in ("temas", "palavras_chave", "participantes", "decisoes"):
        if not isinstance(analise[campo], list):
            raise TypeError(f"{campo} não é lista")
    analise["temas"] = [tema for tema in analise["temas"] if tema in TEMAS][:3]
    if not analise["temas"]:
        raise ValueError("nenhum tema da lista oficial")
    if analise["tipo_sessao"] not in TIPOS_SESSAO:
        analise["tipo_sessao"] = "Outro"
    analise["participantes"] = [
        pessoa for pessoa in analise["participantes"] if isinstance(pessoa, dict) and pessoa.get("nome")
    ][:12]
    analise["palavras_chave"] = [str(termo) for termo in analise["palavras_chave"]][:8]
    analise["decisoes"] = [str(decisao) for decisao in analise["decisoes"]][:8]
    return {campo: analise[campo] for campo in SCHEMA["required"]}


def gera_validado(gerador, sistema: str, usuario: str, tentativas: int = 3) -> dict:
    """Pede o JSON final; resposta inválida gera nova tentativa."""
    mensagens = [{"role": "system", "content": sistema}, {"role": "user", "content": usuario}]
    ultimo_erro: Exception | None = None
    for tentativa in range(1, tentativas + 1):
        try:
            return valida(extrai_json(gerador.gera(mensagens, estruturado=True)))
        except (ValueError, TypeError, KeyError, ErroLLM) as erro:
            ultimo_erro = erro
            tqdm.write(f"  tentativa {tentativa}/{tentativas} falhou: {erro}")
            time.sleep(2 * tentativa)
    raise ErroLLM(f"sem resposta válida após {tentativas} tentativas: {ultimo_erro}")


def gera_topicos(gerador, prompt: str, tentativas: int = 3) -> str | None:
    """Pede os tópicos de uma parte; devolve None se o modelo respondeu NULL (parte sem conteúdo relevante)."""
    ultimo_erro: Exception | None = None
    for tentativa in range(1, tentativas + 1):
        try:
            resposta = gerador.gera([{"role": "user", "content": prompt}], estruturado=False)
        except (KeyError, ErroLLM) as erro:
            ultimo_erro = erro
            tqdm.write(f"  tentativa {tentativa}/{tentativas} falhou: {erro}")
            time.sleep(2 * tentativa)
            continue
        resposta = resposta.split("</think>")[-1].strip()
        if resposta.strip('"').upper() == "NULL":
            return None
        if resposta:
            return resposta
        ultimo_erro = ValueError("resposta vazia")
    raise ErroLLM(f"sem tópicos após {tentativas} tentativas: {ultimo_erro}")


def linhas_da_transcricao(texto: str, palavras_por_linha: int = 30) -> list[str]:
    """Linhas não vazias; texto corrido (linhas enormes) é requebrado para a sobreposição por linhas fazer sentido."""
    linhas = [linha.strip() for linha in texto.splitlines() if linha.strip()]
    resultado = []
    for linha in linhas:
        palavras = linha.split()
        if len(palavras) <= 3 * palavras_por_linha:
            resultado.append(linha)
        else:
            for inicio in range(0, len(palavras), palavras_por_linha):
                resultado.append(" ".join(palavras[inicio : inicio + palavras_por_linha]))
    return resultado


def divide_em_partes(linhas: list[str], max_palavras: int, linhas_sobrepostas: int) -> list[str]:
    """Partes de até `max_palavras`; cada parte começa repetindo as últimas `linhas_sobrepostas` da anterior."""
    partes, atual, palavras = [], [], 0
    for linha in linhas:
        novas = len(linha.split())
        if atual and palavras + novas > max_palavras:
            partes.append("\n".join(atual))
            atual = atual[-linhas_sobrepostas:] if linhas_sobrepostas else []
            palavras = sum(len(anterior.split()) for anterior in atual)
        atual.append(linha)
        palavras += novas
    if atual:
        partes.append("\n".join(atual))
    return partes


def contexto_video(registro: dict | None) -> str:
    if not registro:
        return "Metadados do vídeo: não disponíveis."
    descricao = (registro.get("description") or "").strip()
    if len(descricao) > 1500:
        descricao = descricao[:1500] + " [...]"
    linhas = [
        "Metadados do vídeo no YouTube:",
        f"- Título: {registro.get('title') or ''}",
        f"- Canal: {registro.get('channel_title') or ''}",
        f"- Data de publicação: {str(registro.get('published_at') or '')[:10]}",
    ]
    if registro.get("started_at"):
        linhas.append(f"- Início da transmissão ao vivo: {str(registro['started_at'])[:16]}")
    linhas.append(f"- Duração: {round((registro.get('duration') or 0) / 60)} minutos")
    linhas.append(f"- Descrição: {descricao or '(vazia)'}")
    return "\n".join(linhas)


def analisa(gerador, transcricao: str, registro: dict | None, args, progresso: tqdm) -> tuple[dict, dict]:
    """Devolve a análise final e informações do processamento (partes, partes descartadas, tópicos)."""
    contexto = contexto_video(registro)
    partes = divide_em_partes(linhas_da_transcricao(transcricao), args.max_palavras, args.linhas_sobrepostas)
    if len(partes) == 1:
        analise = gera_validado(gerador, INSTRUCOES, f"{contexto}\n\nTranscrição:\n\n{partes[0]}")
        return analise, {"partes": 1, "partes_sem_conteudo": 0, "topicos": None}
    topicos: list[str] = []
    sem_conteudo = 0
    for numero, parte in enumerate(partes, start=1):
        progresso.set_postfix_str(f"parte {numero}/{len(partes)}")
        if not topicos:
            prompt = PROMPT_PARTE_1.format(
                parte=numero,
                total=len(partes),
                contexto=contexto,
                regras=REGRAS_TOPICOS.format(e_no_resumo=""),
                transcricao=parte,
            )
        else:
            prompt = PROMPT_PARTE_N.format(
                parte=numero,
                total=len(partes),
                contexto=contexto,
                regras=REGRAS_TOPICOS.format(e_no_resumo=" e no resumo preliminar"),
                resumo="\n".join(topicos),
                transcricao=parte,
            )
        resposta = gera_topicos(gerador, prompt)
        if resposta is None:
            sem_conteudo += 1
        else:
            topicos.append(resposta)
    progresso.set_postfix_str("resumo final")
    if not topicos:
        raise ErroLLM("o modelo respondeu NULL para todas as partes")
    usuario = f"{contexto}\n\nTópicos:\n-----\n" + "\n".join(topicos) + "\n-----"
    analise = gera_validado(gerador, INSTRUCOES + "\n\n" + INSTRUCOES_FINAL_TOPICOS, usuario)
    return analise, {"partes": len(partes), "partes_sem_conteudo": sem_conteudo, "topicos": topicos}


def resume(video_id: str, registro: dict | None, pasta_texto: Path, pasta: Path, gerador, args, progresso) -> None:
    """Gera <id>.json com resumo e temas; levanta RuntimeError em caso de falha."""
    remove_temporarios(pasta, video_id)
    transcricao = (pasta_texto / f"{video_id}.txt").read_text(encoding="utf-8")
    inicio = time.perf_counter()
    try:
        analise, info = analisa(gerador, transcricao, registro, args, progresso)
    except ErroLLM as erro:
        raise RuntimeError(str(erro)) from erro
    analise["_meta"] = {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "backend": args.backend,
        "modelo": args.modelo,
        "palavras": len(transcricao.split()),
        **info,
        "segundos": round(time.perf_counter() - inicio, 1),
    }
    temporario = pasta / f"{video_id}.tmp.json"
    temporario.write_text(json.dumps(analise, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporario.rename(pasta / f"{video_id}.json")


# --- execução em paralelo -----------------------------------------------------------------------------------------

FIM = object()


class Etapa:
    """Uma etapa da esteira: uma thread que processa um vídeo por vez e entrega o id para a etapa seguinte.

    `pronto(id)` diz se o arquivo final da etapa já existe; `requisito(id)` diz se o arquivo de entrada existe;
    `faz(id, progresso)` executa o trabalho e levanta RuntimeError se falhar.
    """

    def __init__(self, nome: str, posicao: int, pronto, requisito, faz, max_falhas_seguidas: int | None = 3) -> None:
        self.nome = nome
        self.posicao = posicao
        self.pronto = pronto
        self.requisito = requisito
        self.faz = faz
        self.max_falhas_seguidas = max_falhas_seguidas
        self.entrada: queue.Queue = queue.Queue()
        self.proxima: Etapa | None = None
        self.feitos = self.pulados = self.falhas = 0

    def roda(self, ids: list[str], metadados: dict[str, dict], parar: threading.Event) -> None:
        pendentes = [video_id for video_id in ids if not self.pronto(video_id)]
        total = round(sum(minutos(metadados, video_id) for video_id in pendentes), 1)
        falhas_seguidas = 0
        formato = "{desc}: {percentage:3.0f}%|{bar}| {n:.0f}/{total:.0f} min [{elapsed}<{remaining}{postfix}]"
        with tqdm(
            total=total, desc=f"{self.nome:11}", smoothing=0.1, position=self.posicao, bar_format=formato
        ) as progresso:
            while not parar.is_set():
                try:
                    video_id = self.entrada.get(timeout=0.5)
                except queue.Empty:
                    continue
                if video_id is FIM:
                    break
                if self.pronto(video_id):
                    self.pulados += 1
                elif not self.requisito(video_id):
                    # A etapa anterior falhou para este vídeo (ou não foi pedida e o arquivo não existe).
                    progresso.update(round(minutos(metadados, video_id), 1))
                    continue
                elif self.max_falhas_seguidas is not None and falhas_seguidas >= self.max_falhas_seguidas:
                    progresso.update(round(minutos(metadados, video_id), 1))
                    continue
                else:
                    progresso.set_postfix_str(video_id)
                    try:
                        self.faz(video_id, progresso)
                        self.feitos += 1
                        falhas_seguidas = 0
                    except RuntimeError as erro:
                        self.falhas += 1
                        falhas_seguidas += 1
                        tqdm.write(f"ERRO em {self.nome} de {video_id}: {erro}")
                        if falhas_seguidas == self.max_falhas_seguidas:
                            tqdm.write(f"{self.nome}: {falhas_seguidas} falhas seguidas, etapa interrompida")
                    progresso.update(round(minutos(metadados, video_id), 1))
                    progresso.set_postfix_str("")
                if self.proxima is not None:
                    self.proxima.entrada.put(video_id)
        if self.proxima is not None:
            self.proxima.entrada.put(FIM)


def roda_esteira(etapas: list[Etapa], ids: list[str], metadados: dict[str, dict]) -> None:
    for atual, seguinte in itertools.pairwise(etapas):
        atual.proxima = seguinte
    for video_id in ids:
        etapas[0].entrada.put(video_id)
    etapas[0].entrada.put(FIM)
    parar = threading.Event()
    threads = [threading.Thread(target=etapa.roda, args=(ids, metadados, parar), daemon=True) for etapa in etapas]
    for thread in threads:
        thread.start()
    try:
        while any(thread.is_alive() for thread in threads):
            time.sleep(0.3)
    except KeyboardInterrupt:
        parar.set()
        for thread in threads:
            thread.join(timeout=5)
        raise
    for etapa in etapas:
        tqdm.write(f"{etapa.nome}: {etapa.feitos} feitos, {etapa.pulados} já prontos, {etapa.falhas} com erro")


# --- CLI ----------------------------------------------------------------------------------------------------------


def lista_etapas(valor: str) -> list[str]:
    etapas = [etapa.strip() for etapa in valor.split(",") if etapa.strip()]
    for etapa in etapas:
        if etapa not in ETAPAS:
            raise argparse.ArgumentTypeError(f"etapa desconhecida: {etapa} (opções: {', '.join(ETAPAS)})")
    return etapas


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv", type=Path, help="CSV com a coluna `url`")
    parser.add_argument("--etapas", type=lista_etapas, default=ETAPAS, help="ex.: metadados,audio (padrão: todas)")
    parser.add_argument(
        "--yt-dlp-args", default="", help='opções extras para o yt-dlp, ex.: "--cookies-from-browser firefox"'
    )
    parser.add_argument(
        "--transcritor", choices=["digue", "groq"], default="digue", help="digue (local) ou script da API da Groq"
    )
    parser.add_argument(
        "--groq-comando",
        default=shlex.join([sys.executable, str(Path(__file__).resolve().parent / "groq.py")]),
        help="interpretador + script da Groq (padrão: este Python + groq.py do repositório)",
    )
    parser.add_argument("--groq-modelo", default="whisper-large-v3")
    parser.add_argument("--idioma", default="pt", help="idioma passado ao script da Groq (padrão: pt)")
    parser.add_argument(
        "--apaga-audio", action="store_true", help="apaga o MP3 depois de transcrever, para poupar disco"
    )
    parser.add_argument("--backend", choices=["ollama", "maritaca"], default="ollama")
    parser.add_argument("--modelo", help="nome do modelo no backend escolhido (obrigatório para a etapa resumo)")
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--maritaca-url", default=MARITACA_URL)
    parser.add_argument("--max-palavras", type=int, default=8000, help="tamanho de cada parte (~12k tokens)")
    parser.add_argument("--linhas-sobrepostas", type=int, default=10, help="linhas repetidas entre partes vizinhas")
    parser.add_argument("--temperatura", type=float, default=0.3)
    parser.add_argument("--timeout", type=float, default=1800, help="timeout por chamada ao LLM, em segundos")
    args = parser.parse_args()

    ids = le_csv(args.csv)
    tqdm.write(f"{len(ids)} vídeos em {args.csv}")
    caminho_metadados = PASTA_DADOS / "videos.jsonl"
    pasta_audio = PASTA_DADOS / "audio"
    pasta_transcricao = PASTA_DADOS / "transcricao"
    pasta_resumo = PASTA_DADOS / "resumo"

    try:
        if "metadados" in args.etapas:
            metadados = coleta_metadados(ids, caminho_metadados)
        else:
            metadados = le_jsonl(caminho_metadados)
        etapas = []
        if "audio" in args.etapas:
            pasta_audio.mkdir(parents=True, exist_ok=True)
            extras = shlex.split(args.yt_dlp_args)
            etapas.append(
                Etapa(
                    "audio",
                    len(etapas),
                    # Com --apaga-audio o MP3 some depois da transcrição; o .txt pronto dispensa novo download.
                    pronto=lambda video_id: (
                        (pasta_audio / f"{video_id}.mp3").exists() or (pasta_transcricao / f"{video_id}.txt").exists()
                    ),
                    requisito=lambda video_id: True,
                    faz=lambda video_id, progresso: baixa_audio(video_id, pasta_audio, extras),
                )
            )
        if "transcricao" in args.etapas:
            pasta_transcricao.mkdir(parents=True, exist_ok=True)
            comando_groq = None
            if args.transcritor == "groq":
                comando_groq = [
                    str(Path(parte).expanduser()) if parte.startswith("~") else parte
                    for parte in shlex.split(args.groq_comando)
                ]
                if len(comando_groq) < 2 or not all(Path(parte).exists() for parte in comando_groq[:2]):
                    parser.error(f"--groq-comando aponta para arquivo que não existe: {' '.join(comando_groq)}")
                comando_groq += ["-m", args.groq_modelo, "-l", args.idioma, "transcribe"]
            etapas.append(
                Etapa(
                    "transcricao",
                    len(etapas),
                    pronto=lambda video_id: (pasta_transcricao / f"{video_id}.txt").exists(),
                    requisito=lambda video_id: (pasta_audio / f"{video_id}.mp3").exists(),
                    faz=lambda video_id, progresso: transcreve_e_limpa(
                        video_id,
                        minutos(metadados, video_id),
                        pasta_audio,
                        pasta_transcricao,
                        comando_groq,
                        args.apaga_audio,
                    ),
                )
            )
        if "resumo" in args.etapas:
            if not args.modelo:
                parser.error("--modelo é obrigatório para a etapa resumo")
            if args.backend == "ollama":
                gerador = GeradorOllama(args.ollama_url, args.modelo, args.timeout, args.temperatura)
            else:
                gerador = GeradorMaritaca(args.maritaca_url, args.modelo, args.timeout, args.temperatura)
            pasta_resumo.mkdir(parents=True, exist_ok=True)
            etapas.append(
                Etapa(
                    "resumo",
                    len(etapas),
                    pronto=lambda video_id: (pasta_resumo / f"{video_id}.json").exists(),
                    requisito=lambda video_id: (pasta_transcricao / f"{video_id}.txt").exists(),
                    faz=lambda video_id, progresso: resume(
                        video_id, metadados.get(video_id), pasta_transcricao, pasta_resumo, gerador, args, progresso
                    ),
                )
            )
        if etapas:
            roda_esteira(etapas, ids, metadados)
    except KeyboardInterrupt:
        # Arquivos finais só existem quando completos; temporários (*.tmp.*) são limpos na próxima execução.
        tqdm.write("\ninterrompido; rode de novo para continuar de onde parou")
        sys.exit(130)


if __name__ == "__main__":
    main()
