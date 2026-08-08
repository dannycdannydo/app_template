/**
 * Trigger a browser download for a signed object URL (Scope §6.6).
 *
 * The download URL is fetched first (async, via the generated client in the
 * query layer) and then opened. `window.open` is unreliable after an `await`
 * (popup blockers treat it as outside the user gesture), so the URL is
 * handed to a transient anchor click instead. `noopener` + `_blank` keep the
 * current tab intact; the storage provider's signed URL carries its own
 * Content-Disposition so the download is safe to open in a new tab.
 */
export function triggerDownload(url: string): void {
  const anchor = window.document.createElement('a')
  anchor.href = url
  anchor.rel = 'noopener'
  anchor.target = '_blank'
  anchor.style.display = 'none'
  window.document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
}
