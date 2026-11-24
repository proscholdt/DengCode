/**
 * HTML da grade de prévia de dados (`_dengcode/dataPreview`).
 * As linhas já chegam mascaradas pelo motor; aqui só escapamos e desenhamos.
 */

import { escapeHtml } from '../render/html';
import type { DataPreviewView } from '../chat/viewModel';

export function renderPreviewTable(preview: DataPreviewView): string {
  const head = preview.columns
    .map((c) => `<th title="${escapeHtml(c.type)}">${escapeHtml(c.name)}<span class="col-type">${escapeHtml(c.type)}</span></th>`)
    .join('');
  const body = preview.rows
    .map(
      (row) =>
        `<tr>${row
          .map((cell) => (cell === 'NULL' ? '<td class="null">NULL</td>' : `<td>${escapeHtml(cell)}</td>`))
          .join('')}</tr>`,
    )
    .join('');
  return `<table class="grid"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

export function previewSummary(preview: DataPreviewView): string {
  const shown = preview.rows.length;
  const total = preview.totalRows;
  const totalText = typeof total === 'number' ? `${shown} de ${total.toLocaleString('pt-BR')} linhas` : `${shown} linhas`;
  return `${totalText} · ${preview.columns.length} colunas`;
}

export function renderPreviewPage(options: {
  title: string;
  preview: DataPreviewView;
  csp: string;
  styleUri: string;
}): string {
  const { preview } = options;
  const path = preview.resultPath
    ? `<p class="muted">Resultado completo: <code>${escapeHtml(preview.resultPath)}</code></p>`
    : '';
  return `<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${escapeHtml(options.csp)}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="${escapeHtml(options.styleUri)}">
<title>${escapeHtml(options.title)}</title>
</head>
<body class="panel">
<h2>${escapeHtml(options.title)}</h2>
<p class="muted">${escapeHtml(previewSummary(preview))} · valores sensíveis já mascarados pelo motor</p>
<div class="grid-wrap">${renderPreviewTable(preview)}</div>
${path}
</body>
</html>`;
}
