/**
 * Layout e desenho (SVG) do grafo de lineage de `_dengcode/lineage`.
 *
 * Layout em camadas: a raiz fica na camada 0, quem ela alimenta à direita
 * (+1, +2...) e de quem ela depende à esquerda (−1, −2...). Sem bibliotecas.
 */

import { escapeHtml } from '../render/html';
import type { LineageResult } from '../protocol/acpTypes';

export interface PositionedNode {
  id: string;
  kind: string;
  layer: number;
  x: number;
  y: number;
  width: number;
  height: number;
  isRoot: boolean;
}

export interface LineageLayout {
  width: number;
  height: number;
  nodes: PositionedNode[];
  edges: Array<{ source: string; target: string; via?: string; path: string }>;
}

const NODE_HEIGHT = 30;
const ROW_GAP = 16;
const COL_GAP = 70;
const MARGIN = 20;
const CHAR_WIDTH = 7.2;

/** Camada de cada nó a partir da raiz (BFS ignorando o sentido, com deslocamento assinado). */
export function assignLayers(graph: LineageResult): Map<string, number> {
  const ids = new Set<string>([graph.root, ...graph.nodes.map((n) => n.id)]);
  for (const edge of graph.edges) {
    ids.add(edge.source);
    ids.add(edge.target);
  }
  const layers = new Map<string, number>([[graph.root, 0]]);
  const queue = [graph.root];
  while (queue.length > 0) {
    const current = queue.shift() as string;
    const layer = layers.get(current) ?? 0;
    for (const edge of graph.edges) {
      if (edge.source === current && !layers.has(edge.target)) {
        layers.set(edge.target, layer + 1);
        queue.push(edge.target);
      } else if (edge.target === current && !layers.has(edge.source)) {
        layers.set(edge.source, layer - 1);
        queue.push(edge.source);
      }
    }
  }
  // Nós soltos (sem caminho até a raiz) vão para uma camada extra à direita.
  const maxLayer = Math.max(0, ...layers.values());
  for (const id of ids) {
    if (!layers.has(id)) {
      layers.set(id, maxLayer + 1);
    }
  }
  return layers;
}

export function layoutLineage(graph: LineageResult): LineageLayout {
  const layers = assignLayers(graph);
  const kinds = new Map(graph.nodes.map((n) => [n.id, n.kind]));
  const byLayer = new Map<number, string[]>();
  for (const [id, layer] of layers) {
    const list = byLayer.get(layer) ?? [];
    list.push(id);
    byLayer.set(layer, list);
  }
  const orderedLayers = [...byLayer.keys()].sort((a, b) => a - b);
  const nodes: PositionedNode[] = [];
  let x = MARGIN;
  let maxRows = 0;
  for (const layer of orderedLayers) {
    const ids = (byLayer.get(layer) ?? []).sort((a, b) => a.localeCompare(b));
    const width = Math.max(90, Math.ceil(Math.max(...ids.map((id) => id.length)) * CHAR_WIDTH) + 24);
    ids.forEach((id, row) => {
      nodes.push({
        id,
        kind: kinds.get(id) ?? 'table',
        layer,
        x,
        y: MARGIN + row * (NODE_HEIGHT + ROW_GAP),
        width,
        height: NODE_HEIGHT,
        isRoot: id === graph.root,
      });
    });
    maxRows = Math.max(maxRows, ids.length);
    x += width + COL_GAP;
  }
  const position = new Map(nodes.map((n) => [n.id, n]));
  const edges = graph.edges.flatMap((edge) => {
    const s = position.get(edge.source);
    const t = position.get(edge.target);
    if (!s || !t) {
      return [];
    }
    const x1 = s.x + s.width;
    const y1 = s.y + s.height / 2;
    const x2 = t.x;
    const y2 = t.y + t.height / 2;
    const dx = Math.max(30, Math.abs(x2 - x1) / 2);
    const path = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
    const out: { source: string; target: string; via?: string; path: string } = {
      source: edge.source,
      target: edge.target,
      path,
    };
    if (edge.via) {
      out.via = edge.via;
    }
    return [out];
  });
  const width = Math.max(x - COL_GAP + MARGIN, 200);
  const height = MARGIN * 2 + Math.max(1, maxRows) * (NODE_HEIGHT + ROW_GAP) - ROW_GAP;
  return { width, height, nodes, edges };
}

export function renderLineageSvg(graph: LineageResult): string {
  const layout = layoutLineage(graph);
  const edges = layout.edges
    .map((e) => {
      const title = `${e.source} → ${e.target}${e.via ? ` (via ${e.via})` : ''}`;
      return `<path class="edge" d="${e.path}" marker-end="url(#seta)"><title>${escapeHtml(title)}</title></path>`;
    })
    .join('');
  const nodes = layout.nodes
    .map((n) => {
      const cls = `node node-${n.kind === 'column' ? 'column' : 'table'}${n.isRoot ? ' node-root' : ''}`;
      return (
        `<g class="${cls}" data-id="${escapeHtml(n.id)}">` +
        `<rect x="${n.x}" y="${n.y}" width="${n.width}" height="${n.height}" rx="${n.kind === 'column' ? 14 : 4}"></rect>` +
        `<text x="${n.x + n.width / 2}" y="${n.y + n.height / 2 + 4}" text-anchor="middle">${escapeHtml(n.id)}</text>` +
        `<title>${escapeHtml(`${n.kind}: ${n.id}`)}</title></g>`
      );
    })
    .join('');
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" class="lineage" width="${layout.width}" height="${layout.height}" ` +
    `viewBox="0 0 ${layout.width} ${layout.height}" role="img" aria-label="Lineage de ${escapeHtml(graph.root)}">` +
    '<defs><marker id="seta" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">' +
    '<path d="M 0 0 L 10 5 L 0 10 z" class="arrow"></path></marker></defs>' +
    `${edges}${nodes}</svg>`
  );
}

export function renderLineagePage(options: {
  graph: LineageResult;
  direction: 'down' | 'up';
  csp: string;
  styleUri: string;
}): string {
  const { graph } = options;
  const directionText = options.direction === 'down' ? 'quem depende de' : 'de onde vem';
  const empty = graph.edges.length === 0;
  const body = empty
    ? `<p class="muted">Nenhuma dependência encontrada para <code>${escapeHtml(graph.root)}</code>.</p>`
    : `<div class="lineage-wrap">${renderLineageSvg(graph)}</div>`;
  return `<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${escapeHtml(options.csp)}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="${escapeHtml(options.styleUri)}">
<title>Lineage: ${escapeHtml(graph.root)}</title>
</head>
<body class="panel">
<h2>Lineage de <code>${escapeHtml(graph.root)}</code></h2>
<p class="muted">${escapeHtml(directionText)} ${escapeHtml(graph.root)} · ${graph.nodes.length} nós · ${graph.edges.length} arestas</p>
${body}
</body>
</html>`;
}
