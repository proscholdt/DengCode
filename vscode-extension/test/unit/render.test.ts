import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { parseFromWebview } from '../../src/chat/messages';
import type { ToolItem } from '../../src/chat/viewModel';
import { assignLayers, layoutLineage, renderLineagePage, renderLineageSvg } from '../../src/lineage/lineageSvg';
import { renderPreviewPage, renderPreviewTable } from '../../src/preview/previewHtml';
import { contentSecurityPolicy, escapeHtml, makeNonce } from '../../src/render/html';
import { renderMarkdown } from '../../src/render/markdown';
import { highlightSql } from '../../src/render/sqlHighlight';
import { buildSqlPrompt, hasSqlContent } from '../../src/sql/sqlPrompt';
import { renderItem, renderSqlCard } from '../../src/webview/renderItems';

describe('markdown seguro', () => {
  it('escapa HTML do modelo', () => {
    const html = renderMarkdown('<img src=x onerror=alert(1)> **ok** <script>x</script>');
    assert.ok(!html.includes('<img'));
    assert.ok(!html.includes('<script>'));
    assert.ok(html.includes('&lt;img'));
    assert.ok(html.includes('<strong>ok</strong>'));
  });

  it('só gera links http(s)', () => {
    assert.ok(renderMarkdown('[a](https://x.dev/p)').includes('<a href="https://x.dev/p"'));
    const js = renderMarkdown('[a](javascript:alert(1))');
    assert.ok(!js.includes('<a '));
  });

  it('blocos de código, listas, títulos e citações', () => {
    const html = renderMarkdown('# Título\n\n- um\n- dois\n\n1. a\n2. b\n\n> nota\n\n```sql\nselect 1 -- c\n```\ntexto `x<y`');
    assert.match(html, /<h3>Título<\/h3>/);
    assert.match(html, /<ul><li>um<\/li><li>dois<\/li><\/ul>/);
    assert.match(html, /<ol><li>a<\/li><li>b<\/li><\/ol>/);
    assert.match(html, /<blockquote>nota<\/blockquote>/);
    assert.match(html, /<pre class="code" data-lang="sql"><code><span class="sql-keyword">select<\/span>/);
    assert.match(html, /<code>x&lt;y<\/code>/);
  });

  it('bloco de código aberto (streaming) ainda renderiza', () => {
    assert.match(renderMarkdown('```python\nprint(1)'), /<pre class="code" data-lang="python"><code>print\(1\)<\/code><\/pre>/);
  });
});

describe('realce de SQL', () => {
  it('marca palavras-chave, strings, números e comentários com escape', () => {
    const html = highlightSql("SELECT '<b>' AS x, 42 FROM t -- fim");
    assert.match(html, /<span class="sql-keyword">SELECT<\/span>/);
    assert.match(html, /<span class="sql-string">&#39;&lt;b&gt;&#39;<\/span>/);
    assert.match(html, /<span class="sql-number">42<\/span>/);
    assert.match(html, /<span class="sql-comment">-- fim<\/span>/);
  });
});

describe('itens do chat', () => {
  const tool: ToolItem = {
    kind: 'tool',
    id: 'c1',
    rev: 1,
    toolCallId: 'tc"1',
    title: 'query_run <x>',
    toolKind: 'execute',
    status: 'pending',
    content: [{ type: 'diff', path: '/p/a.sql', isNewFile: false, added: 2, removed: 1 }],
    locations: ['/p/a.sql:3'],
    sql: {
      sql: 'DROP TABLE t',
      kind: 'DDL',
      engine: 'duckdb',
      statements: 1,
      tablesRead: [],
      tablesWritten: ['t'],
      cost: 'US$ 0.01',
      warnings: ['destrutivo'],
      plan: 'SEQ_SCAN',
    },
    permission: {
      permissionId: 'perm-1',
      status: 'pending',
      options: [
        { optionId: 'ok', name: 'Permitir', kind: 'allow_once' },
        { optionId: 'no', name: 'Recusar', kind: 'reject_once' },
      ],
    },
  };

  it('tool call pendente fica aberta com botões de permissão', () => {
    const html = renderItem(tool);
    assert.match(html, /<details class="tool status-pending" open/);
    assert.match(html, /data-action="permission" data-permission="perm-1" data-option="ok"/);
    assert.match(html, /data-testid="sql-card"/);
    assert.match(html, /Ver diff/);
    assert.ok(html.includes('data-tool-id="tc&quot;1"'));
    assert.ok(html.includes('query_run &lt;x&gt;'));
  });

  it('permissão resolvida mostra a decisão', () => {
    const html = renderItem({ ...tool, status: 'completed', permission: { ...tool.permission!, status: 'selected', selectedOptionId: 'no' } });
    assert.match(html, /Recusado: Recusar/);
    assert.ok(!html.includes(' open'));
  });

  it('card de SQL mostra tipo, tabelas, custo, avisos e plano', () => {
    const html = renderSqlCard(tool.sql!);
    assert.match(html, /kind-write">DDL/);
    assert.match(html, /Escreve:.*<code>t<\/code>/);
    assert.match(html, /US\$ 0\.01/);
    assert.match(html, /destrutivo/);
    assert.match(html, /Plano de execução/);
  });

  it('mensagens e avisos são escapados', () => {
    assert.ok(!renderItem({ kind: 'notice', id: 'n', rev: 0, level: 'error', text: '<b>x</b>' }).includes('<b>'));
    assert.match(renderItem({ kind: 'user', id: 'u', rev: 0, text: 'oi' }), /class="msg user"/);
  });
});

describe('prévia de dados', () => {
  it('tabela escapa valores e destaca NULL', () => {
    const html = renderPreviewTable({ columns: [{ name: 'a<', type: 'INT' }], rows: [['<x>'], ['NULL']] });
    assert.ok(html.includes('<th title="INT">a&lt;'));
    assert.ok(html.includes('<td>&lt;x&gt;</td>'));
    assert.ok(html.includes('<td class="null">NULL</td>'));
  });

  it('página tem CSP sem scripts', () => {
    const page = renderPreviewPage({
      title: 'Prévia',
      preview: { columns: [], rows: [], totalRows: 1234, resultPath: '/r.parquet' },
      csp: contentSecurityPolicy('vscode-resource:'),
      styleUri: 'vscode-resource:/panel.css',
    });
    assert.ok(page.includes('script-src &#39;none&#39;'));
    assert.ok(page.includes('0 de 1.234 linhas'));
    assert.ok(page.includes('/r.parquet'));
  });
});

describe('lineage', () => {
  const graph = {
    root: 'silver.pedidos',
    nodes: [
      { id: 'bronze.pedidos', kind: 'table' },
      { id: 'silver.pedidos', kind: 'table' },
      { id: 'gold.vendas', kind: 'table' },
      { id: 'gold.clientes', kind: 'table' },
      { id: 'solto', kind: 'column' },
    ],
    edges: [
      { source: 'bronze.pedidos', target: 'silver.pedidos', via: 'stg' },
      { source: 'silver.pedidos', target: 'gold.vendas' },
      { source: 'silver.pedidos', target: 'gold.clientes' },
    ],
  };

  it('camadas: upstream à esquerda, downstream à direita, soltos no fim', () => {
    const layers = assignLayers(graph);
    assert.equal(layers.get('bronze.pedidos'), -1);
    assert.equal(layers.get('silver.pedidos'), 0);
    assert.equal(layers.get('gold.vendas'), 1);
    assert.equal(layers.get('solto'), 2);
  });

  it('layout posiciona colunas da esquerda para a direita', () => {
    const layout = layoutLineage(graph);
    const x = (id: string): number => layout.nodes.find((n) => n.id === id)?.x ?? -1;
    assert.ok(x('bronze.pedidos') < x('silver.pedidos'));
    assert.ok(x('silver.pedidos') < x('gold.vendas'));
    assert.equal(x('gold.vendas'), x('gold.clientes'));
    assert.equal(layout.edges.length, 3);
    assert.ok(layout.nodes.find((n) => n.id === 'silver.pedidos')?.isRoot);
  });

  it('SVG escapa nomes e marca a raiz', () => {
    const svg = renderLineageSvg({ root: '<r>', nodes: [{ id: '<r>', kind: 'table' }], edges: [{ source: '<r>', target: 'b' }] });
    assert.ok(!svg.includes('<r>'));
    assert.match(svg, /node-root/);
    const page = renderLineagePage({ graph, direction: 'down', csp: contentSecurityPolicy('x'), styleUri: 'x' });
    assert.match(page, /5 nós · 3 arestas/);
    const empty = renderLineagePage({ graph: { root: 'a', nodes: [], edges: [] }, direction: 'up', csp: 'x', styleUri: 'x' });
    assert.match(empty, /Nenhuma dependência/);
  });
});

describe('mensagens da webview', () => {
  it('aceita só mensagens bem formadas', () => {
    assert.deepEqual(parseFromWebview({ type: 'prompt', text: 'oi' }), { type: 'prompt', text: 'oi' });
    assert.equal(parseFromWebview({ type: 'prompt', text: '   ' }), undefined);
    assert.equal(parseFromWebview({ type: 'prompt', text: 3 }), undefined);
    assert.deepEqual(parseFromWebview({ type: 'permission', permissionId: 'p', optionId: null }), {
      type: 'permission',
      permissionId: 'p',
      optionId: null,
    });
    assert.equal(parseFromWebview({ type: 'permission', permissionId: 'p', optionId: 5 }), undefined);
    assert.equal(parseFromWebview({ type: 'rm -rf' }), undefined);
    assert.equal(parseFromWebview('x'), undefined);
    assert.deepEqual(parseFromWebview({ type: 'cancel', extra: 1 }), { type: 'cancel' });
    assert.deepEqual(parseFromWebview({ type: 'rendered', items: 4 }), { type: 'rendered', items: 4 });
    assert.equal(parseFromWebview({ type: 'rendered', items: -1 }), undefined);
    assert.equal(parseFromWebview({ type: 'error', message: 'x'.repeat(5000) }), undefined);
  });
});

describe('HTML e CSP', () => {
  it('escapa os cinco caracteres especiais', () => {
    assert.equal(escapeHtml(`<a href="x">'&'</a>`), '&lt;a href=&quot;x&quot;&gt;&#39;&amp;&#39;&lt;/a&gt;');
  });

  it('CSP com nonce e sem fontes remotas', () => {
    const csp = contentSecurityPolicy('https://file+.vscode-resource.vscode-cdn.net', 'abc');
    assert.match(csp, /default-src 'none'/);
    assert.match(csp, /script-src 'nonce-abc'/);
    assert.ok(!csp.includes('unsafe'));
    assert.equal(makeNonce().length, 32);
  });
});

describe('prompts de SQL', () => {
  it('executar inclui o arquivo e pede aprovação para escrita', () => {
    const p = buildSqlPrompt({ action: 'executar', fileLabel: 'models/a.sql', sql: 'select 1\n\n', isSelection: false });
    assert.match(p, /^Execute o SQL abaixo \(o arquivo `models\/a.sql`\)/);
    assert.match(p, /```sql\nselect 1\n```$/);
  });

  it('explicar com seleção e crases no SQL', () => {
    const p = buildSqlPrompt({ action: 'explicar', fileLabel: 'a.sql', sql: 'select ```x```', isSelection: true });
    assert.match(p, /trecho selecionado/);
    assert.match(p, /sem executá-lo/);
    assert.ok(p.includes('````sql'));
  });

  it('arquivo só com comentários não tem SQL', () => {
    assert.equal(hasSqlContent('-- nada\n/* nada */\n'), false);
    assert.equal(hasSqlContent('select 1'), true);
  });
});
