/**
 * Display formatting helpers for the files UI (Scope §6.6).
 *
 * Pure functions, no dependencies: the files table shows human-readable byte
 * sizes and the upload component shows the same sizes beside the progress
 * bar, so both render paths share one formatter instead of duplicating
 * rounding logic.
 */

/**
 * Format a byte count for display: `0 B`, `512 B`, `2.5 KB`, `10 MB`, ...
 *
 * Exact units (2 KB, 10 MB) stay integer; fractional values keep one decimal
 * so small sizes stay readable.
 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB'] as const
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  const value = bytes / 1024 ** exponent
  const rounded = Number.isInteger(value)
    ? String(value)
    : value >= 10
      ? String(Math.round(value))
      : value.toFixed(1)
  return `${rounded} ${units[exponent]}`
}

/**
 * Format an ISO timestamp for display using the browser's locale. Invalid
 * input is returned unchanged so a malformed server value never renders as
 * "Invalid Date".
 */
export function formatDateTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date)
}
