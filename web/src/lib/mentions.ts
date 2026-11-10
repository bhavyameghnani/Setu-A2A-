// @mentions in the composer. Setu routes on the `mentions` id list, not on the text, so the
// text only needs to look right and the ids must be right.

export type Who = { id: string; handle: string };

const HANDLE = /[A-Za-z0-9][A-Za-z0-9._-]*/;

/** The `@query` being typed at the caret, if any. */
export function activeMention(text: string, caret: number): { start: number; query: string } | null {
  const before = text.slice(0, caret);
  const m = /(^|\s)@([A-Za-z0-9._-]*)$/.exec(before);
  if (!m) return null;
  return { start: before.length - m[2].length - 1, query: m[2] };
}

/** Replace the `@query` that starts at `start` (up to the caret) with `@handle `. */
export function insertMention(text: string, start: number, caret: number, handle: string) {
  const next = `${text.slice(0, start)}@${handle} ${text.slice(caret)}`;
  return { text: next, caret: start + handle.length + 2 };
}

export function suggestions<T extends Who>(people: T[], query: string, limit = 6): T[] {
  const q = query.toLowerCase();
  return people.filter((p) => p.handle.toLowerCase().includes(q)).slice(0, limit);
}

/** Ids of everyone `@named` in the text who is one of `people` (each once, in order). */
export function mentionedIds(text: string, people: Who[]): string[] {
  const byHandle = new Map(people.map((p) => [p.handle.toLowerCase(), p.id]));
  const out: string[] = [];
  for (const m of text.matchAll(new RegExp(`@(${HANDLE.source})`, "g"))) {
    const id = byHandle.get(m[1].replace(/[.-]+$/, "").toLowerCase());
    if (id && !out.includes(id)) out.push(id);
  }
  return out;
}

export type Piece = { text: string; mention?: Who };

/** Split text into plain pieces and recognised @mentions, for highlighting. */
export function splitMentions(text: string, people: Who[]): Piece[] {
  const byHandle = new Map(people.map((p) => [p.handle.toLowerCase(), p]));
  const out: Piece[] = [];
  let last = 0;
  for (const m of text.matchAll(new RegExp(`@(${HANDLE.source})`, "g"))) {
    const who = byHandle.get(m[1].replace(/[.-]+$/, "").toLowerCase());
    if (!who) continue;
    const start = m.index ?? 0;
    const token = `@${who.handle}`;
    if (start > last) out.push({ text: text.slice(last, start) });
    out.push({ text: token, mention: who });
    last = start + token.length;
  }
  if (last < text.length) out.push({ text: text.slice(last) });
  return out;
}
