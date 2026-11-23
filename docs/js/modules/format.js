// Formatting of times and dates.

const parts = (seconds) => [Math.floor(seconds / 3600), Math.floor((seconds % 3600) / 60), Math.floor(seconds % 60)];

export const clock = (seconds) => parts(seconds).map((number) => String(number).padStart(2, "0")).join(":");

export function readableDuration(seconds) {
  const [hours, minutes, rest] = parts(seconds);
  return (hours ? `${hours}h ` : "") + (hours || minutes ? `${minutes}min ` : "") + `${rest}s`;
}

export function readableDate(iso, locale) {
  if (!iso) return "";
  const date = new Date(`${iso}T12:00:00Z`);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleDateString(locale, { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
}
