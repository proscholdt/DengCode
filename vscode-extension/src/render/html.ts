/**
 * Utilitários de HTML seguros, usados pela extensão e pelas webviews.
 */

export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** Nonce aleatório para a CSP das webviews. */
export function makeNonce(random: () => number = Math.random): string {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  let out = '';
  for (let i = 0; i < 32; i++) {
    out += chars.charAt(Math.floor(random() * chars.length));
  }
  return out;
}

/**
 * CSP estrita: nada remoto, scripts só com nonce, estilos só da extensão.
 * `cspSource` é `webview.cspSource`.
 */
export function contentSecurityPolicy(cspSource: string, nonce?: string): string {
  const parts = [
    "default-src 'none'",
    `style-src ${cspSource}`,
    `img-src ${cspSource} data:`,
    `font-src ${cspSource}`,
    nonce ? `script-src 'nonce-${nonce}'` : "script-src 'none'",
  ];
  return parts.join('; ');
}
