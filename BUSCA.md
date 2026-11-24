# Como funciona a busca

O site é estático (GitHub Pages), então a busca roda no navegador. Em vez de baixar todas as transcrições, ele baixa
só os pedaços de um índice invertido que têm as palavras digitadas e, depois, as sessões da página de resultados.

O índice é gerado pelo `transcripts.py site` (seção "search index" do código) e lido por
`site/js/modules/index.js`. Os testes estão em `tests/test_transcripts.py` e `tests/js/index.test.js`.

## Palavras

Texto da transcrição, do título, do resumo e das palavras-chave vira palavras do mesmo jeito em Python
(`index_tokens`) e em JavaScript (`indexTokens`):

- minúsculas e sem acento (decomposição NFD, sem os sinais combinantes): "Orçamento" vira "orcamento";
- sequências de letras e dígitos (`[a-z0-9]+`), o resto separa palavras;
- palavras de uma letra ficam de fora ("a", "e", "o"), como já ficavam na busca.

Não há lista de _stop words_: "de" e "que" estão no índice. Elas aparecem em quase todo bloco, então não ajudam a
escolher sessões, mas custam pouco (a lista de "de" tem 101 KB, 7 KB com _gzip_).

## O índice

Para cada palavra, as sessões onde ela aparece. Cada sessão é identificada pela posição dela em `items.json` (a
lista da página inicial), e para cada uma o índice guarda:

- `tf`: quantas vezes a palavra aparece na transcrição;
- `meta`: quantas vezes aparece no título, no resumo e nas palavras-chave;
- os blocos de 60 segundos da transcrição onde ela aparece (bloco = segundo do início da fala dividido por 60).

Cada palavra vira uma lista plana de números, sessão após sessão:

```
[sessão - sessão anterior, tf, meta, quantidade de blocos, primeiro bloco, bloco - bloco anterior, ...]
```

As diferenças em vez dos valores deixam os números pequenos e repetidos, o que o _gzip_ comprime bem.

Os blocos não guardam a posição da palavra dentro da fala. Por isso o índice sabe se duas palavras estão perto, mas
não se estão em sequência; a sequência é conferida no texto (ver "Conferência dos trechos").

## Particionamento

As palavras ficam em ordem alfabética, cortadas em arquivos de cerca de 40 KB antes da compressão (opção
`--shard-bytes` do `site`). Uma palavra maior que isso ganha um arquivo só para ela. Os arquivos ficam em
`docs/data/index/<versão>/0000.json`, `0001.json` etc., e cada um é um objeto `{palavra: lista}`.

O `items.json`, que a página inicial já baixa, traz em `config.index`:

- `starts`: a primeira palavra de cada arquivo;
- `version`: o nome da pasta;
- `lengths` e `average_length`: o tamanho, em palavras, de cada transcrição e a média (para o BM25);
- `block_seconds`: o tamanho do bloco.

Para achar o arquivo de uma palavra, o navegador faz busca binária em `starts`: é o último arquivo cuja primeira
palavra vem antes dela. Como a busca casa o começo da palavra, ele também baixa os arquivos seguintes que começam
com ela ("ex" pode ocupar mais de um arquivo). Na prática, quase sempre um arquivo por palavra digitada.

Faixas de tamanho parecido em vez de uma letra por arquivo: por letra, "p", "c" e "a" ficavam com uns 600 KB e
centenas de arquivos com menos de 1 KB. Com faixas, o pior caso fica perto do caso médio e cresce pouco com o
acervo, porque o número de arquivos é que aumenta. A simulação que levou a essa escolha está em
`claude/simulacao-indice-particionado.md`, no projeto.

## Versão e _cache_

A pasta tem o nome de um _hash_ do conteúdo dos arquivos. Cada `make site` apaga a pasta anterior e cria outra; se
nada mudou, o nome é o mesmo.

Assim um navegador nunca mistura o `items.json` de uma publicação com arquivos de outra: se o `items.json` em
memória ou em _cache_ aponta para uma versão que não existe mais, o arquivo responde 404, o site baixa o
`items.json` de novo sem _cache_ e repete a busca uma vez.

Não adianta tentar manter os arquivos entre publicações: o GitHub Pages troca o `ETag` de todos os arquivos a cada
_deploy_, e uma única sessão nova muda quase todos os arquivos do índice (medido: 1 sessão muda 89% a 100% dos
_bytes_).

## Consulta

O texto da busca vira termos como antes (`queryTerms`): o que está entre aspas é um termo só, o resto é separado
por espaço. Cada termo vira palavras do índice (`queryWords`):

- palavra solta: casa pelo começo ("orcament" acha "orcamento", "orcamentaria");
- frase entre aspas: as palavras de dentro casam inteiras e só a última casa pelo começo. Em "projeto de lei", "de"
  só acha "de", e não "deputado", "depois" etc.; isso evita baixar dezenas de arquivos por causa de uma palavra
  curta.

Uma sessão entra no resultado quando tem todas as palavras e uma destas condições vale:

- na transcrição, as palavras estão no mesmo bloco de 60 s ou em blocos vizinhos (para cada bloco da palavra mais
  rara, as outras estão no mesmo bloco, no anterior ou no seguinte);
- todas as palavras estão no título, no resumo ou nas palavras-chave.

Os filtros (casa, tema, datas) valem depois, sobre essa lista.

## Ranqueamento

BM25, com `k1 = 1,2` e `b = 0,75`. Para cada palavra da consulta, somam-se as notas de cada palavra do índice que
ela casa (quem busca "orcament" soma "orcamento" e "orcamentaria", cada uma com o seu peso):

```
idf  = ln(1 + (N - n + 0,5) / (n + 0,5))
f    = tf + 3 × meta
nota = idf × f × (k1 + 1) / (f + k1 × (1 - b + b × tamanho / tamanho médio))
```

- `N`: sessões no índice; `n`: sessões com aquela palavra do índice;
- `tamanho`: palavras da transcrição da sessão; o tamanho médio vem do `items.json`;
- uma ocorrência no título, no resumo ou nas palavras-chave vale 3 da transcrição.

A nota da sessão é a soma das notas das palavras. Empate: a mais recente (posição na lista) vem antes.

Antes era pelo número de trechos achados. Com o BM25, uma sessão longa que cita o termo muitas vezes de passagem
pesa menos, e um termo raro pesa mais que um comum.

## Conferência dos trechos

Os trechos mostrados nos cards vêm do texto, não do índice. Para as 20 sessões da página, o site baixa os arquivos
`data/items/<uuid>.json` e procura os termos como na página da sessão: todos na mesma fala ou em duas falas
seguidas, ignorando acento e caixa, com a frase entre aspas inteira. Cada card mostra:

- "N trechos", quando acha;
- "termo no título ou no resumo", quando não acha na fala mas o índice achou no resumo;
- "termos próximos, fora da mesma fala", quando as palavras estão a até um bloco de distância mas não na mesma
  fala nem em duas seguidas; o card mostra as falas daqueles blocos que têm alguma das palavras.

As sessões da página ficam em memória para trocar de página e abrir uma sessão sem novo _download_ (as últimas 60).

## O que muda em relação à busca anterior

A busca anterior baixava todas as transcrições (40 MB) na primeira busca e procurava cada termo como pedaço de texto.

- Casamento pelo começo da palavra, não pelo meio: "lombola" não acha mais "quilombola".
- O topo mostra o número de sessões, sem o total de trechos: contar trechos exigiria o texto de todas as sessões.
- Com duas ou mais palavras, o número de sessões é maior que o da busca anterior: entram sessões em que as palavras
  estão a até um bloco de distância, mesmo fora da mesma fala.

Comparação com 35 consultas, no acervo de teste (311 sessões, 452 h):

| consultas | sessões da busca anterior que o índice acha | sessões do índice que a busca anterior também acha |
|---|---|---|
| 1 palavra (11) | 96,5% | 100% |
| 2 palavras (11) | 91,4% | 74% |
| 3 palavras ou mais (10) | 100% | 43% |
| frase entre aspas (3) | 100% | 93% |

Das 48 sessões perdidas com 2 palavras, 45 só casavam no meio de uma palavra.

## Tamanhos e tempos

Medido no acervo de teste (311 sessões, 452 h), em Chrome, com o subcomando `measure`:

- índice: 57.421 palavras em 274 arquivos, 9,8 MB (2,9 MB com _gzip_); arquivo mediano com 11 KB comprimido;
- `items.json`: 10 KB a mais com _gzip_, por causa de `starts` e `lengths`;
- primeira busca ("orçamento") no perfil celular (9 Mbit/s, 170 ms, CPU 4x): de 10,1 s e 6,1 MB para 1,8 s e
  699 KB; memória do JavaScript de 61 MB para 9 MB;
- no computador, de 0,88 s para 0,31 s.

Quase todo o _download_ de uma busca são os arquivos das 20 sessões da página (cerca de 650 KB), não o índice.
