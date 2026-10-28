// Split a generated answer into plain-text runs and `[n]` citation markers so the
// UI can render each marker as a clickable control that jumps to source n. Only
// markers whose number is in `known` become citations; a stray `[7]` with no
// matching citation is left as literal text.

export type AnswerSegment = { type: "text"; value: string } | { type: "cite"; number: number };

const MARKER = /\[(\d+)\]/g;

export function parseAnswer(text: string, known: ReadonlySet<number>): AnswerSegment[] {
  const segments: AnswerSegment[] = [];
  let lastIndex = 0;

  for (const match of text.matchAll(MARKER)) {
    const number = Number(match[1]);
    const start = match.index;
    if (!known.has(number)) continue; // leave unknown markers as literal text
    if (start > lastIndex) {
      segments.push({ type: "text", value: text.slice(lastIndex, start) });
    }
    segments.push({ type: "cite", number });
    lastIndex = start + match[0].length;
  }

  if (lastIndex < text.length) {
    segments.push({ type: "text", value: text.slice(lastIndex) });
  }
  return segments;
}
