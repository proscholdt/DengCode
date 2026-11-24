/**
 * Painéis de editor sem script: prévia de dados e lineage (HTML/SVG estático, CSP estrita).
 */

import * as vscode from 'vscode';
import type { DataPreviewView } from './chat/viewModel';
import { renderLineagePage } from './lineage/lineageSvg';
import type { LineageDirection, LineageResult } from './protocol/acpTypes';
import { renderPreviewPage } from './preview/previewHtml';
import { contentSecurityPolicy } from './render/html';

function createPanel(extensionUri: vscode.Uri, viewType: string, title: string): vscode.WebviewPanel {
  return vscode.window.createWebviewPanel(
    viewType,
    title,
    { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true },
    { enableScripts: false, localResourceRoots: [vscode.Uri.joinPath(extensionUri, 'media')] },
  );
}

export class Panels {
  private previewPanel: vscode.WebviewPanel | undefined;
  private lineagePanel: vscode.WebviewPanel | undefined;

  constructor(private readonly extensionUri: vscode.Uri) {}

  private styleUri(panel: vscode.WebviewPanel): string {
    return panel.webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, 'media', 'panel.css')).toString();
  }

  showPreview(title: string, preview: DataPreviewView): vscode.WebviewPanel {
    if (!this.previewPanel) {
      this.previewPanel = createPanel(this.extensionUri, 'dengcode.preview', title);
      this.previewPanel.onDidDispose(() => (this.previewPanel = undefined));
    }
    const panel = this.previewPanel;
    panel.title = title;
    panel.webview.html = renderPreviewPage({
      title,
      preview,
      csp: contentSecurityPolicy(panel.webview.cspSource),
      styleUri: this.styleUri(panel),
    });
    panel.reveal(vscode.ViewColumn.Beside, true);
    return panel;
  }

  showLineage(graph: LineageResult, direction: LineageDirection): vscode.WebviewPanel {
    const title = `Lineage: ${graph.root}`;
    if (!this.lineagePanel) {
      this.lineagePanel = createPanel(this.extensionUri, 'dengcode.lineage', title);
      this.lineagePanel.onDidDispose(() => (this.lineagePanel = undefined));
    }
    const panel = this.lineagePanel;
    panel.title = title;
    panel.webview.html = renderLineagePage({
      graph,
      direction,
      csp: contentSecurityPolicy(panel.webview.cspSource),
      styleUri: this.styleUri(panel),
    });
    panel.reveal(vscode.ViewColumn.Beside, true);
    return panel;
  }

  get currentLineagePanel(): vscode.WebviewPanel | undefined {
    return this.lineagePanel;
  }

  get currentPreviewPanel(): vscode.WebviewPanel | undefined {
    return this.previewPanel;
  }
}
