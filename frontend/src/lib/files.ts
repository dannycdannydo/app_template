import type { components } from '@/api/generated/openapi'

type FileStatus = components['schemas']['FileStatus']

/**
 * File-status display metadata (Scope §6.6, blueprint §16 design system).
 *
 * One map drives the `FileStatusBadge` component: the human label and the
 * semantic-token tone classes. Tones use only the template's semantic tokens
 * (background/primary/secondary/destructive/muted/accent/chart-*) so a dark
 * theme keeps working without hand-picked colours (blueprint §16: no
 * arbitrary colours).
 */
export interface FileStatusMeta {
  label: string
  className: string
}

export const FILE_STATUS_META: Record<FileStatus, FileStatusMeta> = {
  pending: { label: 'Pending', className: 'bg-muted text-muted-foreground' },
  uploaded: { label: 'Uploaded', className: 'bg-accent text-accent-foreground' },
  processing: { label: 'Processing', className: 'bg-chart-4/15 text-chart-4' },
  ready: { label: 'Ready', className: 'bg-primary/10 text-primary' },
  failed: { label: 'Failed', className: 'bg-destructive/10 text-destructive' },
  quarantined: { label: 'Quarantined', className: 'bg-destructive/10 text-destructive' },
  deleted: { label: 'Deleted', className: 'bg-muted text-muted-foreground line-through' },
}

/** Fallback for a status value the API has not taught the UI about yet. */
const UNKNOWN_STATUS_META: FileStatusMeta = {
  label: 'Unknown',
  className: 'bg-muted text-muted-foreground',
}

export function fileStatusMeta(status: string): FileStatusMeta {
  return FILE_STATUS_META[status as FileStatus] ?? UNKNOWN_STATUS_META
}
