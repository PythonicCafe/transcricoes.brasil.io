// Busca no navegador: normalização, análise da consulta, destaque dos termos e varredura das transcrições.

export const escapa = (texto) => String(texto ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export const normaliza = (texto) => texto.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

// Frases entre aspas viram um termo só; palavras de uma letra são ignoradas.
export function termosDaConsulta(consulta) {
  const termos = [];
  const resto = consulta.replace(/"([^"]+)"/g, (_, frase) => { termos.push(normaliza(frase.trim())); return " "; });
  for (const palavra of resto.split(/\s+/)) if (palavra.trim().length > 1) termos.push(normaliza(palavra.trim()));
  return termos.filter(Boolean);
}

const VARIANTES = { a: "aáàâãä", e: "eéèêë", i: "iíìîï", o: "oóòôõö", u: "uúùûü", c: "cç", n: "nñ" };

// Devolve HTML seguro com os termos dentro de <mark>, sem diferenciar acento nem caixa.
export function destaca(texto, termos) {
  if (!termos.length) return escapa(texto);
  const padroes = termos.map((termo) => [...termo].map((letra) =>
    VARIANTES[letra] ? `[${VARIANTES[letra]}]` : letra.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join(""));
  const expressao = new RegExp(`(${padroes.join("|")})`, "gi");
  return texto.split(expressao).map((pedaco, posicao) => posicao % 2 ? `<mark>${escapa(pedaco)}</mark>` : escapa(pedaco)).join("");
}

// Posições das falas que casam com os termos: todos na mesma fala, ou em duas falas seguidas (aí `inteira` é falso
// e o trecho vai até a fala seguinte, que é pulada).
export function falasQueCasam(linhas, termos) {
  const achadas = [];
  for (let posicao = 0; posicao < linhas.length; posicao++) {
    const janela = linhas[posicao] + " " + (linhas[posicao + 1] || "");
    if (termos.every((termo) => janela.includes(termo)) && termos.some((termo) => linhas[posicao].includes(termo))) {
      const inteira = termos.every((termo) => linhas[posicao].includes(termo));
      achadas.push({ posicao, inteira });
      if (!inteira) posicao++;
    }
  }
  return achadas;
}

// Procura em todas as sessões recebidas; `dados` é o `busca.json` ({falas, palavras_chave}, por id). Um trecho casa
// quando todos os termos estão na mesma fala ou em duas falas seguidas. Sessões sem trecho entram se os termos
// estiverem no título, no resumo ou nas palavras-chave.
export function busca(sessoes, dados, termos, cache = {}) {
  const resultados = [];
  for (const sessao of sessoes) {
    const original = dados.falas[sessao.id] || [];
    const linhas = cache[sessao.id] ??= original.map((linha) => normaliza(linha[1]));
    const trechos = falasQueCasam(linhas, termos).map(({ posicao, inteira }) => ({
      segundos: original[posicao][0],
      texto: inteira ? original[posicao][1] : original[posicao][1] + " " + original[posicao + 1][1],
    }));
    const meta = normaliza(`${sessao.titulo} ${sessao.resumo} ${(dados.palavras_chave[sessao.id] || []).join(" ")}`);
    const noResumo = termos.every((termo) => meta.includes(termo));
    if (trechos.length || noResumo) resultados.push({ sessao, trechos, noResumo });
  }
  resultados.sort((a, b) => b.trechos.length - a.trechos.length);
  return resultados;
}
