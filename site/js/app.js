// Ponto de entrada do site: estado, rotas (pelo hash da URL) e desenho das páginas.
import { TEXTOS } from "./modules/textos.js";
import { busca, destaca, escapa, falasQueCasam, normaliza, termosDaConsulta } from "./modules/busca.js";
import { dataLegivel, duracaoLegivel, relogio } from "./modules/formato.js";

function guardado(chave, padrao) {
  try { return localStorage.getItem(chave) || padrao; } catch (erro) { return padrao; }
}
function guarda(chave, valor) {
  try { localStorage.setItem(chave, valor); } catch (erro) { /* a escolha vale só nesta visita */ }
}

let idioma = guardado("idioma", "en");
if (!TEXTOS[idioma]) idioma = "en";
let temaVisual = guardado("tema", "sistema");
let indice = null;          // {config, sessoes: [...]}
let transcricoes = null;    // {id: [[segundos, texto], ...]}
let carregando = null;
const cacheDeLinhas = {};
const estado = { consulta: "", casas: new Set(), tema: "", de: "", ate: "" };

const t = (chave, valores = {}) => {
  let texto = TEXTOS[idioma][chave] ?? chave;
  for (const [nome, valor] of Object.entries(valores)) texto = texto.replaceAll(`{${nome}}`, valor);
  return texto;
};
const data = (iso) => dataLegivel(iso, TEXTOS[idioma].local);
const app = () => document.getElementById("app");

function aplicaTemaVisual() {
  const escuro = temaVisual === "escuro" || (temaVisual === "sistema" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.setAttribute("data-bs-theme", escuro ? "dark" : "light");
}

function carregaTranscricoes() {
  if (!carregando) carregando = fetch("dados/transcricoes.json").then((r) => r.json()).then((dados) => { transcricoes = dados; });
  return carregando;
}

function sessoesFiltradas() {
  return indice.sessoes.filter((sessao) =>
    (!estado.casas.size || estado.casas.has(sessao.casa)) &&
    (!estado.tema || sessao.temas.includes(estado.tema)) &&
    (!estado.de || sessao.data >= estado.de) &&
    (!estado.ate || sessao.data <= estado.ate));
}

const origem = (sessao) => `${escapa(sessao.casa)}${sessao.uf ? " (" + escapa(sessao.uf) + ")" : ""}`;
const temasHtml = (sessao) => sessao.temas.map((tema) => `<span class="theme-badge" lang="pt-BR">${escapa(tema)}</span>`).join(" ");

function cartao(sessao, termos, extra = "") {
  const destino = `#/v/${encodeURIComponent(sessao.id)}`;
  return `<article class="result-card">
    <a class="result-card__thumb" href="${destino}" tabindex="-1" aria-hidden="true"><img class="result-card__image" src="${escapa(sessao.miniatura)}" width="320" height="180" loading="lazy" alt=""></a>
    <div class="result-card__body">
      <h2 class="result-card__title" lang="pt-BR"><a href="${destino}">${destaca(sessao.titulo, termos)}</a></h2>
      <p class="result-card__meta"><span class="result-card__source" lang="pt-BR">${origem(sessao)}</span>, ${t("publicado")} ${data(sessao.data)} · <span lang="pt-BR">${escapa(sessao.tipo)}</span></p>
      <p class="result-card__label">${t("sumarizacao")}</p>
      <p class="result-card__summary" lang="pt-BR">${destaca(sessao.resumo, termos)}</p>
      <p class="result-card__footer"><span>${duracaoLegivel(sessao.duracao)}</span> ${temasHtml(sessao)}</p>
      ${extra}
    </div></article>`;
}

function rotuloCasas() {
  if (!estado.casas.size) return t("todas_casas");
  return estado.casas.size === 1 ? escapa([...estado.casas][0]) : t("casas_escolhidas", { n: estado.casas.size });
}

function desenhaInicio() {
  const casas = [...new Set(indice.sessoes.map((s) => s.casa))].sort((a, b) => a.localeCompare(b, "pt"));
  const temas = [...new Set(indice.sessoes.flatMap((s) => s.temas))].sort((a, b) => a.localeCompare(b, "pt"));
  const horas = Math.round(indice.sessoes.reduce((soma, s) => soma + s.duracao, 0) / 3600);
  app().innerHTML = `
    <h1 class="page-title">${t("titulo")}</h1>
    <p class="page-lead">${t("subtitulo", { sessoes: indice.sessoes.length, casas: casas.length, horas })}</p>
    <form class="search-form" id="formulario" role="search">
      <div class="search-form__query">
        <label class="visually-hidden" for="consulta">${t("rotulo_busca")}</label>
        <input id="consulta" class="form-control form-control-lg" type="search" placeholder="${escapa(t("buscar"))}" value="${escapa(estado.consulta)}">
      </div>
      <button class="btn btn-lg search-form__submit" type="submit">${t("botao_buscar")}</button>
      <details class="multi-select search-form__filter" id="casas">
        <summary class="form-select multi-select__summary" id="casas-rotulo">${rotuloCasas()}</summary>
        <fieldset class="multi-select__options">
          <legend class="visually-hidden">${t("todas_casas")}</legend>
          ${casas.map((casa) => `<label class="form-check multi-select__option"><input class="form-check-input" type="checkbox" value="${escapa(casa)}" ${estado.casas.has(casa) ? "checked" : ""}> <span class="form-check-label" lang="pt-BR">${escapa(casa)}</span></label>`).join("")}
        </fieldset>
      </details>
      <div class="search-form__filter">
        <label class="visually-hidden" for="tema">${t("todos_temas")}</label>
        <select id="tema" class="form-select"><option value="">${t("todos_temas")}</option>${temas.map((c) => `<option ${c === estado.tema ? "selected" : ""}>${escapa(c)}</option>`).join("")}</select>
      </div>
      <div class="input-group search-form__date"><label class="input-group-text" for="de">${t("de")}</label><input id="de" class="form-control" type="date" value="${escapa(estado.de)}"></div>
      <div class="input-group search-form__date"><label class="input-group-text" for="ate">${t("ate")}</label><input id="ate" class="form-control" type="date" value="${escapa(estado.ate)}"></div>
    </form>
    <div id="resultados" aria-live="polite"></div>`;
  const aplica = (evento) => {
    if (evento && evento.type === "submit") evento.preventDefault();
    estado.consulta = document.getElementById("consulta").value.trim();
    estado.tema = document.getElementById("tema").value;
    estado.de = document.getElementById("de").value;
    estado.ate = document.getElementById("ate").value;
    estado.casas = new Set([...document.querySelectorAll("#casas input:checked")].map((caixa) => caixa.value));
    document.getElementById("casas-rotulo").innerHTML = rotuloCasas();
    desenhaResultados();
  };
  document.getElementById("formulario").addEventListener("submit", aplica);
  for (const id of ["tema", "de", "ate", "casas"]) document.getElementById(id).addEventListener("change", aplica);
  desenhaResultados();
}

function trechosHtml(sessao, trechos, termos) {
  return trechos.map((trecho) => `<li class="passage-list__item"><a class="time-link" href="#/v/${encodeURIComponent(sessao.id)}/${Math.floor(trecho.segundos)}">▶ ${relogio(trecho.segundos)}</a> <span lang="pt-BR">${destaca(trecho.texto, termos)}</span></li>`).join("");
}

function desenhaResultados() {
  const alvo = document.getElementById("resultados");
  const termos = termosDaConsulta(estado.consulta);
  if (!termos.length) {
    const sessoes = sessoesFiltradas();
    alvo.innerHTML = `<p class="result-count">${sessoes.length} ${t("sessoes")}</p>` + sessoes.map((sessao) => cartao(sessao, [])).join("");
    return;
  }
  if (!transcricoes) {
    alvo.innerHTML = `<p class="result-count">${t("carregando")}</p>`;
    carregaTranscricoes().then(desenhaResultados);
    return;
  }
  const resultados = busca(sessoesFiltradas(), transcricoes, termos, cacheDeLinhas);
  const total = resultados.reduce((soma, r) => soma + r.trechos.length, 0);
  if (!resultados.length) { alvo.innerHTML = `<p class="empty-state">${t("nenhum")}</p>`; return; }
  alvo.innerHTML = `<p class="result-count">${t("resultados", { trechos: total, sessoes: resultados.length })} <strong>${escapa(estado.consulta)}</strong></p>` +
    resultados.map(({ sessao, trechos }, numero) => cartao(sessao, termos, `
        <p class="result-card__label">${trechos.length ? trechos.length + " " + t("trechos") : t("so_resumo")}</p>
        <ul class="passage-list" id="trechos-${numero}">${trechosHtml(sessao, trechos.slice(0, 5), termos)}</ul>
        ${trechos.length > 5 ? `<button class="btn btn-link btn-sm passage-list__more" type="button" data-mais="${numero}">${t("mais", { n: trechos.length })}</button>` : ""}`)).join("");
  alvo.querySelectorAll("[data-mais]").forEach((botao) => botao.addEventListener("click", () => {
    const { sessao, trechos } = resultados[Number(botao.dataset.mais)];
    document.getElementById(`trechos-${botao.dataset.mais}`).innerHTML = trechosHtml(sessao, trechos, termos);
    botao.remove();
  }));
}

// `enablejsapi` e `origin` permitem que a API de iframe do YouTube informe o tempo atual do vídeo.
function enderecoPlayer(id, segundos, tocar) {
  return `https://www.youtube-nocookie.com/embed/${encodeURIComponent(id)}?start=${Math.floor(segundos)}${tocar ? "&autoplay=1" : ""}&enablejsapi=1&origin=${encodeURIComponent(location.origin)}`;
}

let apiDoPlayer = null;     // promessa da API de iframe do YouTube, carregada na primeira sessão aberta
let player = null;          // player da sessão aberta, quando a API está pronta
let relogioDoPlayer = null; // intervalo que acompanha o tempo do vídeo
let sessaoAberta = 0;       // muda a cada sessão desenhada, para descartar respostas de uma sessão anterior

function carregaApiDoPlayer() {
  if (!apiDoPlayer) {
    apiDoPlayer = new Promise((resolve, reject) => {
      window.onYouTubeIframeAPIReady = () => resolve(window.YT);
      const script = document.createElement("script");
      script.src = "https://www.youtube.com/iframe_api";
      script.onerror = reject;
      document.head.appendChild(script);
    });
  }
  return apiDoPlayer;
}

function paraPlayer() {
  clearInterval(relogioDoPlayer);
  relogioDoPlayer = null;
  player = null;
  sessaoAberta++;
}

function desenhaSessao(id, segundos) {
  const sessao = indice.sessoes.find((s) => s.id === id);
  if (!sessao) { location.hash = "#/"; return; }
  const vez = sessaoAberta;
  const erro = `${indice.config.repositorio}/issues/new?title=${encodeURIComponent("Erro em " + id + ": " + sessao.titulo)}`;
  const lista = (itens) => itens.length ? `<ul class="machine-box__list" lang="pt-BR">${itens.map((item) => `<li>${item}</li>`).join("")}</ul>` : "";
  // O YouTube recusa o player (erro 153) quando a página não envia o cabeçalho Referer; daí o referrerpolicy.
  app().innerHTML = `
    <p><a href="#/">${t("voltar")}</a></p>
    <h1 class="page-title page-title--session" lang="pt-BR">${escapa(sessao.titulo)}</h1>
    <p class="page-lead"><span lang="pt-BR">${origem(sessao)}</span>, ${t("publicado")} ${data(sessao.data)} · ${duracaoLegivel(sessao.duracao)} · <span lang="pt-BR">${escapa(sessao.tipo)}</span> ·
      <a href="https://www.youtube.com/watch?v=${encodeURIComponent(id)}" target="_blank" rel="noopener">${t("ver_youtube")}</a> · <a href="${escapa(sessao.legenda)}" download>${t("baixar_legenda")}</a> · <a href="${escapa(erro)}" target="_blank" rel="noopener">${t("erro")}</a></p>
    <div class="session-layout">
      <div class="session-layout__main">
        <div class="video-player ratio ratio-16x9"><iframe id="player" src="${enderecoPlayer(id, segundos || 0, segundos !== null)}" title="YouTube: ${escapa(sessao.titulo)}" referrerpolicy="strict-origin-when-cross-origin" allow="autoplay; encrypted-media; picture-in-picture; web-share" allowfullscreen></iframe></div>
        <section class="machine-box" aria-labelledby="titulo-resumo">
          <h2 class="machine-box__label" id="titulo-resumo">${t("resumo_maquina")} · ${t("modelo")} ${escapa(sessao.modelo)}</h2>
          <p lang="pt-BR">${escapa(sessao.resumo)}</p>
          <p>${temasHtml(sessao)}</p>
          ${sessao.palavras_chave.length ? `<p class="machine-box__detail"><strong>${t("palavras")}:</strong> <span lang="pt-BR">${sessao.palavras_chave.map(escapa).join(", ")}</span></p>` : ""}
          ${sessao.participantes.length ? `<div class="machine-box__detail"><strong>${t("participantes")}:</strong>${lista(sessao.participantes.map((p) => `${escapa(p.nome)}${p.papel ? " (" + escapa(p.papel) + ")" : ""}`))}</div>` : ""}
          <div class="machine-box__detail"><strong>${t("decisoes")}:</strong> ${sessao.decisoes.length ? lista(sessao.decisoes.map(escapa)) : t("sem_decisoes")}</div>
          <p class="machine-box__warning">${t("aviso_maquina")}</p>
        </section>
      </div>
      <section class="session-layout__side" aria-labelledby="titulo-transcricao">
        <h2 class="machine-box__label" id="titulo-transcricao">${t("transcricao_maquina")}</h2>
        <div class="transcript-search">
          <label class="visually-hidden" for="busca-sessao">${t("buscar_sessao")}</label>
          <input id="busca-sessao" class="form-control" type="search" placeholder="${escapa(t("buscar_sessao"))}" value="${escapa(estado.consulta)}">
          <span class="transcript-search__count" id="contagem-sessao" aria-live="polite"></span>
        </div>
        <p class="transcript-hint">${t("clique")}</p>
        <ol class="transcript-list" id="transcricao" tabindex="0" lang="pt-BR"><li>${t("carregando")}</li></ol>
      </section>
    </div>`;
  carregaTranscricoes().then(() => {
    const linhas = transcricoes[id] || [];
    const alvo = document.getElementById("transcricao");
    if (!alvo) return;
    const normalizadas = cacheDeLinhas[id] ??= linhas.map((linha) => normaliza(linha[1]));
    const campo = document.getElementById("busca-sessao");
    const contagem = document.getElementById("contagem-sessao");
    let atual = -1;
    if (segundos !== null) linhas.forEach((linha, posicao) => { if (linha[0] <= segundos) atual = posicao; });
    // Marca a fala `posicao` como atual, se ela estiver na lista (com filtro, pode não estar). A lista só rola
    // sozinha quando o ponteiro não está sobre ela, para não brigar com quem está lendo.
    const marca = (posicao, rolar) => {
      atual = posicao;
      const anterior = alvo.querySelector(".is-current");
      const nova = alvo.querySelector(`[data-linha="${posicao}"]`);
      if (anterior === nova) return;
      if (anterior) anterior.classList.remove("is-current");
      if (!nova) return;
      nova.classList.add("is-current");
      if (rolar && !alvo.matches(":hover")) alvo.scrollTop = nova.offsetTop - alvo.offsetTop - 60;
    };
    // Com termos no campo, a lista mostra só as falas que casam (e a seguinte, quando o trecho passa de uma fala).
    const desenhaLinhas = () => {
      const termos = termosDaConsulta(campo.value);
      let posicoes = linhas.map((_, posicao) => posicao);
      if (termos.length) {
        const achadas = falasQueCasam(normalizadas, termos);
        posicoes = achadas.flatMap(({ posicao, inteira }) => inteira ? [posicao] : [posicao, posicao + 1]);
        contagem.textContent = t("ocorrencias", { n: achadas.length });
      } else {
        contagem.textContent = "";
      }
      alvo.innerHTML = posicoes.length
        ? posicoes.map((posicao) => `<li class="transcript-list__line" data-linha="${posicao}"><button class="time-link" type="button" data-segundos="${linhas[posicao][0]}" data-posicao="${posicao}">▶ ${relogio(linhas[posicao][0])}</button> ${destaca(linhas[posicao][1], termos)}</li>`).join("")
        : `<li>${t("nenhum_na_sessao")}</li>`;
      alvo.scrollTop = 0;
      marca(atual, true);
    };
    campo.addEventListener("input", () => { estado.consulta = campo.value.trim(); desenhaLinhas(); });
    alvo.addEventListener("click", (evento) => {
      const botao = evento.target.closest("[data-segundos]");
      if (!botao) return;
      const inicioDaFala = Number(botao.dataset.segundos);
      if (player) {
        player.seekTo(inicioDaFala, true);
        player.playVideo();
      } else {
        document.getElementById("player").src = enderecoPlayer(id, inicioDaFala, true);
      }
      marca(Number(botao.dataset.posicao), false);
    });
    desenhaLinhas();

    // Enquanto o vídeo toca, acompanha o tempo e marca a fala correspondente. Sem a API do YouTube (bloqueada,
    // por exemplo), o resto da página segue funcionando, só sem a marcação automática.
    carregaApiDoPlayer().then((YT) => {
      if (vez !== sessaoAberta || !document.getElementById("player")) return;
      const novo = new YT.Player("player", { events: { onReady: () => { if (vez === sessaoAberta) player = novo; } } });
      relogioDoPlayer = setInterval(() => {
        if (vez !== sessaoAberta || !player || player.getPlayerState() !== YT.PlayerState.PLAYING) return;
        const agora = player.getCurrentTime();
        let primeira = 0, ultima = linhas.length - 1, posicao = -1;
        while (primeira <= ultima) {
          const meio = (primeira + ultima) >> 1;
          if (linhas[meio][0] <= agora) { posicao = meio; primeira = meio + 1; } else { ultima = meio - 1; }
        }
        if (posicao !== atual) marca(posicao, true);
      }, 500);
    }).catch(() => {});
  });
}

function desenhaSobre() {
  const casas = new Set(indice.sessoes.map((s) => s.casa)).size;
  const horas = Math.round(indice.sessoes.reduce((soma, s) => soma + s.duracao, 0) / 3600);
  const modelos = [...new Set(indice.sessoes.map((s) => s.modelo))].map((modelo) => `${escapa(modelo)} (${t("pesos_abertos")})`).join(", ");
  const valores = { sessoes: indice.sessoes.length, horas, casas, modelos, repo: escapa(indice.config.repositorio) };
  app().innerHTML = `<p><a href="#/">${t("voltar")}</a></p><h1 class="page-title">${t("sobre_titulo")}</h1><div class="prose-block">` +
    TEXTOS[idioma].sobre.map((paragrafo) => {
      let texto = paragrafo;
      for (const [nome, valor] of Object.entries(valores)) texto = texto.replaceAll(`{${nome}}`, valor);
      return `<p>${texto}</p>`;
    }).join("") + "</div>";
}

function desenha() {
  document.documentElement.lang = TEXTOS[idioma].local;
  document.title = `${t("marca")} - Brasil.IO`;
  document.querySelectorAll("[data-t]").forEach((el) => { el.textContent = t(el.dataset.t); });
  document.querySelectorAll("[data-t-rotulo]").forEach((el) => { el.setAttribute("aria-label", t(el.dataset.tRotulo)); });
  document.querySelectorAll("[data-idioma]").forEach((botao) => { botao.setAttribute("aria-pressed", String(botao.dataset.idioma === idioma)); });
  document.querySelectorAll("[data-tema-visual]").forEach((botao) => { botao.setAttribute("aria-pressed", String(botao.dataset.temaVisual === temaVisual)); });
  if (!indice) return;
  document.getElementById("link-repo").href = indice.config.repositorio;
  const partes = decodeURIComponent(location.hash.replace(/^#\/?/, "")).split("/");
  window.scrollTo(0, 0);
  paraPlayer();
  if (partes[0] === "v" && partes[1]) desenhaSessao(partes[1], partes[2] !== undefined && partes[2] !== "" ? Number(partes[2]) : null);
  else if (partes[0] === "sobre") desenhaSobre();
  else desenhaInicio();
}

window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", aplicaTemaVisual);
document.addEventListener("click", (evento) => {
  const botaoIdioma = evento.target.closest("[data-idioma]");
  if (botaoIdioma) {
    idioma = botaoIdioma.dataset.idioma;
    guarda("idioma", idioma);
    desenha();
    return;
  }
  const botaoTema = evento.target.closest("[data-tema-visual]");
  if (botaoTema) {
    temaVisual = botaoTema.dataset.temaVisual;
    guarda("tema", temaVisual);
    aplicaTemaVisual();
    document.querySelectorAll("[data-tema-visual]").forEach((botao) => { botao.setAttribute("aria-pressed", String(botao === botaoTema)); });
    return;
  }
  const aberto = document.querySelector("details.multi-select[open]");
  if (aberto && !aberto.contains(evento.target)) aberto.removeAttribute("open");
});
window.addEventListener("hashchange", desenha);
aplicaTemaVisual();
desenha();
fetch("dados/indice.json").then((r) => r.json()).then((dados) => { indice = dados; desenha(); })
  .catch(() => { app().innerHTML = "<p class=\"empty-state\">Could not load dados/indice.json.</p>"; });
