/**
 * Download do binário do motor (segue redirecionamentos do GitHub Releases).
 */

import * as fs from 'node:fs';
import * as https from 'node:https';
import * as path from 'node:path';

export class DownloadNotFoundError extends Error {
  constructor(readonly url: string) {
    super(`binário não encontrado na release (${url})`);
    this.name = 'DownloadNotFoundError';
  }
}

export interface DownloadOptions {
  onProgress?: (received: number, total: number | undefined) => void;
  signal?: AbortSignal;
  maxRedirects?: number;
}

function get(url: string, options: DownloadOptions, redirectsLeft: number): Promise<import('node:http').IncomingMessage> {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { headers: { 'User-Agent': 'dengcode-vscode', Accept: 'application/octet-stream' } }, (res) => {
      const status = res.statusCode ?? 0;
      if (status >= 300 && status < 400 && res.headers.location) {
        res.resume();
        if (redirectsLeft <= 0) {
          reject(new Error('redirecionamentos demais ao baixar o motor'));
          return;
        }
        const next = new URL(res.headers.location, url).toString();
        get(next, options, redirectsLeft - 1).then(resolve, reject);
        return;
      }
      if (status === 404) {
        res.resume();
        reject(new DownloadNotFoundError(url));
        return;
      }
      if (status !== 200) {
        res.resume();
        reject(new Error(`falha ao baixar o motor: HTTP ${status}`));
        return;
      }
      resolve(res);
    });
    req.on('error', reject);
    options.signal?.addEventListener('abort', () => req.destroy(new Error('download cancelado')), { once: true });
  });
}

/** Baixa `url` para `destination` (via arquivo temporário) e marca como executável. */
export async function downloadFile(url: string, destination: string, options: DownloadOptions = {}): Promise<void> {
  await fs.promises.mkdir(path.dirname(destination), { recursive: true });
  const response = await get(url, options, options.maxRedirects ?? 5);
  const total = Number(response.headers['content-length']) || undefined;
  const temp = `${destination}.part`;
  await new Promise<void>((resolve, reject) => {
    const out = fs.createWriteStream(temp);
    let received = 0;
    response.on('data', (chunk: Buffer) => {
      received += chunk.length;
      options.onProgress?.(received, total);
    });
    response.on('error', reject);
    out.on('error', reject);
    out.on('finish', resolve);
    response.pipe(out);
  });
  await fs.promises.rename(temp, destination);
  if (process.platform !== 'win32') {
    await fs.promises.chmod(destination, 0o755);
  }
}
