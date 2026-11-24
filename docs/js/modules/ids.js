// Document ids and the source URLs they come from.

// YouTube video id of a canonical URL (`https://youtube.com/watch?v=<id>`); null for other sources.
export function youtubeId(url) {
  try {
    const parsed = new URL(url);
    return parsed.hostname.replace(/^www\./, "") === "youtube.com" ? parsed.searchParams.get("v") : null;
  } catch (error) {
    return null;
  }
}
