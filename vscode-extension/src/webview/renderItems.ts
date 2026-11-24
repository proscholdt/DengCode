/**
 * HTML de cada item do chat. Puro (sem DOM) para poder ser testado em Node.
 * Todo texto vindo do motor passa por `escapeHtml` ou pelo markdown seguro.
 */

import type { ChatItem, PermissionView, SqlCardView, ToolItem } from '../chat/viewModel';
import { escapeHtml } from '../render/html';
import { renderMarkdown } from '../render/markdown';
import { highlightSql } from '../render/sqlHighlight';
import { previewSummary, renderPreviewTable } from '../preview/previewHtml';

const STATUS_LABEL: Record<ToolItem['status'], string> = {
  pending: 'pendente',
  in_progress: 'executando',
  completed: 'concluída',
  failed: 'falhou',
};

const KIND_ICON: Record<string, string> = {
  read: '📄',
  edit: '✏️',
  delete: '🗑️',
  move: '↔️',
  search: '🔎',
  execute: '▶',
  think: '💭',
  fetch: '🌐',
  other: '🔧',
};

function attr(value: string): string {
  return escapeHtml(value);
}

export function renderSqlCard(sql: SqlCardView): string {
  const kindClass = /^(read|select)$/i.test(sql.kind) ? 'kind-read' : 'kind-write';
  const meta: string[] = [`<span class="badge ${kindClass}">${escapeHtml(sql.kind)}</span>`];
  if (sql.engine) {
    meta.push(`<span class="badge">${escapeHtml(sql.engine)}</span>`);
  }
  if (typeof sql.statements === 'number') {
    meta.push(`<span class="muted">${sql.statements} instrução(ões)</span>`);
  }
  if (sql.cost) {
    meta.push(`<span class="cost" title="Custo estimado">💲 ${escapeHtml(sql.cost)}</span>`);
  }
  const tables = (label: string, list: string[]): string =>
    list.length > 0
      ? `<div class="tables"><span class="muted">${label}:</span> ${list.map((t) => `<code>${escapeHtml(t)}</code>`).join(' ')}</div>`
      : '';
  const warnings =
    sql.warnings.length > 0
      ? `<ul class="warnings">${sql.warnings.map((w) => `<li>⚠️ ${escapeHtml(w)}</li>`).join('')}</ul>`
      : '';
  const plan = sql.plan
    ? `<details class="plan"><summary>Plano de execução</summary><pre class="code">${escapeHtml(sql.plan)}</pre></details>`
    : '';
  return (
    `<div class="sql-card" data-testid="sql-card">` +
    `<div class="sql-meta">${meta.join(' ')}</div>` +
    `<pre class="code sql"><code>${highlightSql(sql.sql)}</code></pre>` +
    tables('Lê', sql.tablesRead) +
    tables('Escreve', sql.tablesWritten) +
    warnings +
    plan +
    `</div>`
  );
}

export function renderPermission(permission: PermissionView): string {
  if (permission.status !== 'pending') {
    const chosen = permission.options.find((o) => o.optionId === permission.selectedOptionId);
    const text =
      permission.status === 'cancelled'
        ? 'Pedido cancelado.'
        : chosen?.kind.startsWith('allow')
          ? `Aprovado: ${chosen.name}`
          : `Recusado: ${chosen?.name ?? permission.selectedOptionId ?? ''}`;
    return `<div class="permission resolved" data-testid="permissao-resolvida">${escapeHtml(text)}</div>`;
  }
  const buttons = permission.options
    .map((o) => {
      const cls = o.kind.startsWith('allow') ? (o.kind === 'allow_once' ? 'primary' : 'secondary') : 'danger';
      return (
        `<button type="button" class="${cls}" data-action="permission" data-permission="${attr(permission.permissionId)}" ` +
        `data-option="${attr(o.optionId)}" data-testid="btn-permissao-${attr(o.kind)}">${escapeHtml(o.name)}</button>`
      );
    })
    .join('');
  return `<div class="permission pending" data-testid="permissao"><span>O DengCode pede aprovação:</span><div class="buttons">${buttons}</div></div>`;
}

export function renderTool(item: ToolItem): string {
  const icon = KIND_ICON[item.toolKind] ?? KIND_ICON.other;
  const content = item.content
    .map((c) => {
      switch (c.type) {
        case 'text':
          return `<div class="tool-text">${renderMarkdown(c.text)}</div>`;
        case 'diff':
          return (
            `<div class="diff-summary"><code>${escapeHtml(c.path)}</code> ` +
            `<span class="added">+${c.added}</span> <span class="removed">−${c.removed}</span>` +
            `${c.isNewFile ? ' <span class="badge">novo</span>' : ''} ` +
            `<button type="button" class="link" data-action="openDiff" data-tool="${attr(item.toolCallId)}">Ver diff</button></div>`
          );
        case 'terminal':
          return `<div class="muted">Terminal ${escapeHtml(c.terminalId)}</div>`;
        default:
          return `<div class="muted">[${escapeHtml(c.label)}]</div>`;
      }
    })
    .join('');
  const locations =
    item.locations.length > 0
      ? `<div class="locations">${item.locations
          .map((l) => `<button type="button" class="link" data-action="openLocation" data-location="${attr(l)}">${escapeHtml(l)}</button>`)
          .join(' ')}</div>`
      : '';
  const preview = item.preview
    ? `<div class="preview" data-testid="previa-dados"><div class="preview-head"><span class="muted">${escapeHtml(previewSummary(item.preview))}</span> ` +
      `<button type="button" class="link" data-action="openPreview" data-tool="${attr(item.toolCallId)}">Abrir em painel</button></div>` +
      `<div class="grid-wrap">${renderPreviewTable(item.preview)}</div></div>`
    : '';
  const sql = item.sql ? renderSqlCard(item.sql) : '';
  const permission = item.permission ? renderPermission(item.permission) : '';
  // Pedidos pendentes ficam abertos; o resto começa recolhido.
  const open = item.permission?.status === 'pending' || item.status === 'failed' ? ' open' : '';
  return (
    `<details class="tool status-${item.status}"${open} data-tool-id="${attr(item.toolCallId)}">` +
    `<summary><span class="icon">${icon}</span> <span class="title">${escapeHtml(item.title)}</span> ` +
    `<span class="tool-status">${STATUS_LABEL[item.status]}</span></summary>` +
    `<div class="tool-body">${sql}${content}${locations}${preview}</div>` +
    `</details>${permission}`
  );
}

export function renderItem(item: ChatItem): string {
  switch (item.kind) {
    case 'user':
      return `<div class="msg user">${renderMarkdown(item.text)}</div>`;
    case 'agent':
      return `<div class="msg agent">${renderMarkdown(item.text)}</div>`;
    case 'thought':
      return `<details class="thought"><summary>Raciocínio</summary>${renderMarkdown(item.text)}</details>`;
    case 'tool':
      return renderTool(item);
    case 'plan':
      return (
        `<div class="plan-list"><div class="muted">Plano</div><ol>${item.entries
          .map((e) => `<li class="plan-${attr(e.status)}">${e.status === 'completed' ? '✔ ' : e.status === 'in_progress' ? '▶ ' : ''}${escapeHtml(e.content)}</li>`)
          .join('')}</ol></div>`
      );
    case 'notice':
      return `<div class="notice ${item.level}">${escapeHtml(item.text)}</div>`;
  }
}
