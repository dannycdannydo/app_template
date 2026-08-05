#!/usr/bin/env node
/**
 * Regenerate the TypeScript API client from the backend OpenAPI schema (blueprint §15).
 *
 * Two steps:
 * 1. `openapi-typescript` converts `backend/openapi.json` (exported by the
 *    backend script `scripts/export_openapi.py`) into `src/api/generated/openapi.d.ts`.
 * 2. Prettier formats the output. The programmatic API is used because it
 *    bypasses `.prettierignore`, which deliberately excludes the generated
 *    directory from global `pnpm format` runs.
 */
import { execFileSync } from 'node:child_process'
import { readFile, writeFile } from 'node:fs/promises'
import prettier from 'prettier'

const repoRoot = new URL('../../', import.meta.url)
const specPath = new URL('backend/openapi.json', repoRoot).pathname
const outputPath = new URL('frontend/src/api/generated/openapi.d.ts', repoRoot).pathname

execFileSync('openapi-typescript', [specPath, '-o', outputPath], { stdio: 'inherit' })

const source = await readFile(outputPath, 'utf8')
const config = await prettier.resolveConfig(outputPath)
const formatted = await prettier.format(source, { ...config, filepath: outputPath })
await writeFile(outputPath, formatted)
