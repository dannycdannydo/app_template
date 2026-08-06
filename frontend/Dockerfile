# Frontend image (blueprint §36, ADR-0008).
#
# The frontend is a static build artefact: this image compiles the Vue
# application and serves the result from nginx with an API reverse proxy. The
# same image serves CI (container build validation) and the full-container
# `make dev-docker` path.

# --- Build stage: compile the Vue application -------------------------------
FROM node:24-alpine AS build

WORKDIR /app

# npm/pnpm registry override for networks where registry.npmjs.org is slow or
# blocked (e.g. mirrors like https://registry.npmmirror.com). Pass with:
#   --build-arg NPM_REGISTRY=https://registry.npmmirror.com
ARG NPM_REGISTRY=https://registry.npmjs.org

# pnpm pinned to the version used locally so installs are reproducible.
RUN npm install --global --registry=${NPM_REGISTRY} pnpm@11.13.0

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile --registry=${NPM_REGISTRY}

COPY . .
# Vite substitutes these public values while compiling the static bundle. They
# must be build arguments; runtime environment variables cannot change them.
ARG VITE_API_BASE_URL=
ARG VITE_WORKOS_CLIENT_ID=
ARG VITE_WORKOS_REDIRECT_URI=
RUN VITE_API_BASE_URL=${VITE_API_BASE_URL} \
    VITE_WORKOS_CLIENT_ID=${VITE_WORKOS_CLIENT_ID} \
    VITE_WORKOS_REDIRECT_URI=${VITE_WORKOS_REDIRECT_URI} \
    pnpm build

# --- Serve stage: static files behind nginx with API proxy ------------------
FROM nginx:alpine AS serve

# Refresh the Alpine packages shipped in the base image so the runtime is
# patched for the latest high/critical CVEs (checked by the container scan).
RUN apk upgrade --no-cache

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
