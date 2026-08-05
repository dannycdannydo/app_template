# Root Makefile — v0.1 command surface (blueprint §32).
#
# Targets are added per work unit as the release progresses. Scope §6.8
# completes the full surface (dev, migrate, lint, typecheck, test, format,
# check); this file currently carries only the targets already owned by
# completed work units.

.PHONY: generate-client

## Regenerate the TypeScript API client from the backend's OpenAPI schema.
## Exports openapi.json from the FastAPI app, then runs openapi-typescript.
generate-client:
	cd backend && uv run python -m scripts.export_openapi --output openapi.json
	cd frontend && pnpm generate:client
