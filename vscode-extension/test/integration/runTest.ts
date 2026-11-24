/**
 * Roda os testes de integração dentro de um VS Code real (baixado pelo
 * @vscode/test-electron) com o motor fake no lugar do `dengcode`.
 * No Linux sem tela, rode com `xvfb-run -a`.
 */
import * as path from 'node:path';
import { runTests } from '@vscode/test-electron';

async function main(): Promise<void> {
  // out/test/integration → raiz da extensão
  const extensionDevelopmentPath = path.resolve(__dirname, '..', '..', '..');
  const extensionTestsPath = path.resolve(__dirname, 'suite', 'index');
  const workspace = path.resolve(extensionDevelopmentPath, 'test', 'fixtures', 'workspace');
  const fakeEngine = path.resolve(extensionDevelopmentPath, 'test', 'fake-engine', 'fake-dengcode.mjs');
  // Quando o runner é chamado de dentro de outro VS Code (terminal integrado,
  // agentes), essas variáveis fariam o VS Code de teste rodar como Node.
  for (const key of Object.keys(process.env)) {
    if (key === 'ELECTRON_RUN_AS_NODE' || key.startsWith('VSCODE_')) {
      delete process.env[key];
    }
  }
  try {
    await runTests({
      version: process.env.VSCODE_TEST_VERSION ?? 'stable',
      extensionDevelopmentPath,
      extensionTestsPath,
      launchArgs: [workspace, '--disable-extensions', '--disable-workspace-trust', '--skip-welcome', '--skip-release-notes'],
      extensionTestsEnv: { DENGCODE_FAKE_ENGINE: fakeEngine },
    });
  } catch (error) {
    console.error('Falha nos testes de integração:', error);
    process.exit(1);
  }
}

void main();
