/**
 * Markdown "suficiente" para o chat, com escape de tudo o que vem do modelo.
 *
 * Suporta: blocos de código (```), código inline, títulos, listas, citações,
 * negrito, itálico e links http(s). Nunca repassa HTML cru. Funciona bem com
 * texto incompleto (streaming): um bloco de código aberto é desenhado como
 * código até fechar.
 */

import { escapeHtml } from './html';
import { highlightSql } from './sqlHighlight';

function renderInline(text: string): string {
  // Separa código inline antes de aplicar o resto.
  const parts = text.split(/(`[^`\n]+`)/g);
  return parts
    .map((part) => {
      if (part.length >= 2 && part.startsWith('`') && part.endsWith('`')) {
        return `<code>${escapeHtml(part.slice(1, -1))}</code>`;
      }
      let html = escapeHtml(part);
      html = html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
      html = html.replace(/(^|[^*\w])\*([^*\n]+)\*(?![*\w])/g, '$1<em>$2</em>');
      html = html.replace(/(^|[^_\w])_([^_\n]+)_(?![_\w])/g, '$1<em>$2</em>');
      // Links: o texto e a URL já estão escapados; só aceitamos http(s).
      html = html.replace(
        /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
        (_m, label: string, url: string) => `<a href="${url}" title="${url}">${label}</a>`,
      );
      return html;
    })
    .join('');
}

function renderCode(code: string, lang: string): string {
  const normalized = lang.trim().toLowerCase();
  const body = normalized === 'sql' ? highlightSql(code) : escapeHtml(code);
  const langAttr = normalized ? ` data-lang="${escapeHtml(normalized)}"` : '';
  return `<pre class="code"${langAttr}><code>${body}</code></pre>`;
}

export function renderMarkdown(source: string): string {
  const lines = source.replace(/\r\n/g, '\n').split('\n');
  const out: string[] = [];
  let paragraph: string[] = [];
  let list: { ordered: boolean; items: string[] } | undefined;
  let quote: string[] = [];

  const flushParagraph = (): void => {
    if (paragraph.length > 0) {
      out.push(`<p>${paragraph.map(renderInline).join('<br>')}</p>`);
      paragraph = [];
    }
  };
  const flushList = (): void => {
    if (list) {
      const tag = list.ordered ? 'ol' : 'ul';
      out.push(`<${tag}>${list.items.map((i) => `<li>${renderInline(i)}</li>`).join('')}</${tag}>`);
      list = undefined;
    }
  };
  const flushQuote = (): void => {
    if (quote.length > 0) {
      out.push(`<blockquote>${quote.map(renderInline).join('<br>')}</blockquote>`);
      quote = [];
    }
  };
  const flushAll = (): void => {
    flushParagraph();
    flushList();
    flushQuote();
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i] ?? '';
    const fence = /^\s*```(\w[\w+-]*)?\s*$/.exec(line);
    if (fence) {
      flushAll();
      const lang = fence[1] ?? '';
      const code: string[] = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i] ?? '')) {
        code.push(lines[i] ?? '');
        i++;
      }
      out.push(renderCode(code.join('\n'), lang));
      continue;
    }
    if (line.trim().length === 0) {
      flushAll();
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushAll();
      const level = Math.min((heading[1] ?? '#').length + 2, 6); // h3..h6: o chat é estreito
      out.push(`<h${level}>${renderInline(heading[2] ?? '')}</h${level}>`);
      continue;
    }
    const bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (bullet || numbered) {
      flushParagraph();
      flushQuote();
      const ordered = Boolean(numbered);
      if (list && list.ordered !== ordered) {
        flushList();
      }
      list ??= { ordered, items: [] };
      list.items.push((bullet ?? numbered)?.[1] ?? '');
      continue;
    }
    const quoted = /^\s*>\s?(.*)$/.exec(line);
    if (quoted) {
      flushParagraph();
      flushList();
      quote.push(quoted[1] ?? '');
      continue;
    }
    flushList();
    flushQuote();
    paragraph.push(line);
  }
  flushAll();
  return out.join('\n');
}
