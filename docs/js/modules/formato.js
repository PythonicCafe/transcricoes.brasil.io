// Formatação de tempos e datas.

const partes = (segundos) => [Math.floor(segundos / 3600), Math.floor((segundos % 3600) / 60), Math.floor(segundos % 60)];

export const relogio = (segundos) => partes(segundos).map((n) => String(n).padStart(2, "0")).join(":");

export function duracaoLegivel(segundos) {
  const [h, m, s] = partes(segundos);
  return (h ? `${h}h ` : "") + (h || m ? `${m}min ` : "") + `${s}s`;
}

export function dataLegivel(iso, local) {
  if (!iso) return "";
  const data = new Date(`${iso}T12:00:00Z`);
  return Number.isNaN(data.getTime()) ? iso : data.toLocaleDateString(local, { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
}
