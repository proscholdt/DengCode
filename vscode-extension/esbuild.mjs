// Build da extensão com esbuild.
//   node esbuild.mjs               → dist/ (extensão + webview), com sourcemaps
//   node esbuild.mjs --production  → dist/ minificado, sem sourcemaps
//   node esbuild.mjs --watch       → recompila ao salvar
//   node esbuild.mjs --tests       → out/test/ (testes de integração com @vscode/test-electron)
import * as fs from 'node:fs';
import * as esbuild from 'esbuild';

const production = process.argv.includes('--production');
const watch = process.argv.includes('--watch');
const tests = process.argv.includes('--tests');

/** @type {esbuild.BuildOptions[]} */
const builds = tests
  ? [
      {
        entryPoints: ['test/integration/runTest.ts', 'test/integration/suite/index.ts', 'test/integration/suite/extension.test.ts'],
        outdir: 'out/test/integration',
        outbase: 'test/integration',
        bundle: true,
        platform: 'node',
        format: 'cjs',
        target: 'node20',
        sourcemap: true,
        external: ['vscode', 'mocha', '@vscode/test-electron'],
      },
    ]
  : [
      {
        entryPoints: ['src/extension.ts'],
        outfile: 'dist/extension.js',
        bundle: true,
        platform: 'node',
        format: 'cjs',
        target: 'node20',
        external: ['vscode'],
        sourcemap: !production,
        minify: production,
      },
      {
        entryPoints: ['src/webview/chat.ts'],
        outfile: 'dist/webview/chat.js',
        bundle: true,
        platform: 'browser',
        format: 'iife',
        target: 'es2022',
        sourcemap: !production,
        minify: production,
      },
    ];

// Build de produção parte de um dist/ limpo (sem sourcemaps antigos).
if (production) {
  fs.rmSync('dist', { recursive: true, force: true });
}

if (watch) {
  for (const options of builds) {
    const ctx = await esbuild.context(options);
    await ctx.watch();
  }
  console.log('esbuild: observando alterações…');
} else {
  await Promise.all(builds.map((options) => esbuild.build(options)));
  console.log(`esbuild: build ${tests ? 'dos testes' : production ? 'de produção' : 'de desenvolvimento'} concluído`);
}
