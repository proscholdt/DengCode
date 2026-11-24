/**
 * CodeLens em arquivos `.sql`: "Executar com DengCode" e "Explicar".
 */

import * as vscode from 'vscode';
import { hasSqlContent } from './sqlPrompt';

export class SqlCodeLensProvider implements vscode.CodeLensProvider {
  provideCodeLenses(document: vscode.TextDocument): vscode.CodeLens[] {
    if (!hasSqlContent(document.getText())) {
      return [];
    }
    // Primeira linha com conteúdo (pula comentários de cabeçalho em branco).
    let line = 0;
    while (line < document.lineCount - 1 && document.lineAt(line).isEmptyOrWhitespace) {
      line++;
    }
    const range = new vscode.Range(line, 0, line, 0);
    return [
      new vscode.CodeLens(range, {
        title: '▶ Executar com DengCode',
        command: 'dengcode.executarSql',
        arguments: [document.uri],
        tooltip: 'Envia o SQL (ou a seleção) para o DengCode executar, com aprovação quando precisar',
      }),
      new vscode.CodeLens(range, {
        title: 'Explicar',
        command: 'dengcode.explicarSql',
        arguments: [document.uri],
        tooltip: 'Pede ao DengCode para explicar o SQL sem executá-lo',
      }),
    ];
  }
}
