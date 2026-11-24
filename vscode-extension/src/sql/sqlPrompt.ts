/**
 * Prompts enviados pelo CodeLens/comandos de SQL. Puro (sem VS Code).
 */

export type SqlAction = 'executar' | 'explicar';

export interface SqlPromptInput {
  action: SqlAction;
  /** Caminho relativo ao workspace (ou nome do arquivo). */
  fileLabel: string;
  sql: string;
  isSelection: boolean;
}

/** Cerca de código que não colide com crases dentro do SQL. */
function fence(sql: string): string {
  const longest = Math.max(2, ...Array.from(sql.matchAll(/`+/g), (m) => m[0].length));
  return '`'.repeat(longest + 1);
}

export function buildSqlPrompt(input: SqlPromptInput): string {
  const sql = input.sql.replace(/\s+$/, '');
  const origin = input.isSelection ? `o trecho selecionado de \`${input.fileLabel}\`` : `o arquivo \`${input.fileLabel}\``;
  const f = fence(sql);
  const block = `${f}sql\n${sql}\n${f}`;
  if (input.action === 'executar') {
    return (
      `Execute o SQL abaixo (${origin}) com a ferramenta de consulta e mostre uma prévia do resultado. ` +
      `Se for escrita, DDL ou DML, peça aprovação antes.\n\n${block}`
    );
  }
  return (
    `Explique o que o SQL abaixo faz (${origin}), sem executá-lo: tabelas lidas e escritas, ` +
    `filtros, junções, grão do resultado e riscos (custo, performance, dados sensíveis).\n\n${block}`
  );
}

/** Uma linha de CodeLens por instrução seria ruído: usamos o topo do arquivo. */
export function hasSqlContent(text: string): boolean {
  const withoutComments = text.replace(/--[^\n]*/g, '').replace(/\/\*[\s\S]*?\*\//g, '');
  return withoutComments.trim().length > 0;
}
