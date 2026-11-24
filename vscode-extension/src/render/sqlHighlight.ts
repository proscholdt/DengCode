/**
 * Realce simples de SQL (palavras-chave, strings, números, comentários).
 * Escapa tudo; só acrescenta `<span class="sql-*">`.
 */

import { escapeHtml } from './html';

const KEYWORDS = new Set(
  `select from where and or not in is null as join left right inner outer full cross on group by order
  having limit offset union all distinct insert into values update set delete create table view
  alter drop add column with case when then else end asc desc exists between like ilike
  merge using matched replace truncate grant revoke primary key references default if
  partition over window qualify cast interval true false schema database index materialized
  returning explain analyze describe show`
    .split(/\s+/)
    .filter((k) => k.length > 0),
);

const TOKEN = /(--[^\n]*|\/\*[\s\S]*?\*\/|'(?:[^']|'')*'?|"(?:[^"]|"")*"?|\b\d+(?:\.\d+)?\b|\b[A-Za-z_][A-Za-z0-9_]*\b)/g;

export function highlightSql(sql: string): string {
  let out = '';
  let last = 0;
  for (const match of sql.matchAll(TOKEN)) {
    const token = match[0];
    const index = match.index ?? 0;
    out += escapeHtml(sql.slice(last, index));
    last = index + token.length;
    let cls: string | undefined;
    if (token.startsWith('--') || token.startsWith('/*')) {
      cls = 'sql-comment';
    } else if (token.startsWith("'")) {
      cls = 'sql-string';
    } else if (token.startsWith('"')) {
      cls = 'sql-ident';
    } else if (/^\d/.test(token)) {
      cls = 'sql-number';
    } else if (KEYWORDS.has(token.toLowerCase())) {
      cls = 'sql-keyword';
    }
    out += cls ? `<span class="${cls}">${escapeHtml(token)}</span>` : escapeHtml(token);
  }
  out += escapeHtml(sql.slice(last));
  return out;
}
