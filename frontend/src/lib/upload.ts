/**
 * Direct-upload transport (Scope §6.6).
 *
 * The browser PUTs the file straight to the signed URL issued by the
 * upload-intent endpoint (blueprint §17 direct upload flow). That request is
 * deliberately not a generated-client call: the signed URL is opaque to the
 * app and may be cross-origin (MinIO locally, the storage provider's host in
 * production), so it goes through a plain `XMLHttpRequest`, which exposes the
 * per-byte progress events `fetch` still lacks. `onProgress` receives the
 * XHR's length-computable values so the UI can render a real progress bar.
 *
 * Only `2xx` statuses resolve; MinIO answers signed PUTs with `200`. The
 * upload URL is the storage provider's own signed artifact, so no
 * credentials are attached — the signature in the URL is the auth.
 */

export interface UploadProgress {
  loaded: number
  total: number
}

export function putFile(
  url: string,
  file: File,
  onProgress?: (progress: UploadProgress) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('PUT', url)

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        onProgress?.({ loaded: event.loaded, total: event.total })
      }
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve()
      } else {
        reject(new Error(`Upload failed with status ${xhr.status}`))
      }
    }
    xhr.onerror = () => {
      reject(new Error('Upload failed (network error)'))
    }
    xhr.onabort = () => {
      reject(new Error('Upload aborted'))
    }

    xhr.send(file)
  })
}
