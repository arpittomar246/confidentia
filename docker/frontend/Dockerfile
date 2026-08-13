# The dashboard: built with Node, served by nginx.
#
# Multi-stage because the runtime needs none of it. The build stage pulls ~150
# packages and a toolchain; the result is 400 kB of static assets, and shipping the
# builder would put npm and a package manager inside a container whose only job is
# to return files.
FROM node:22-alpine AS build

WORKDIR /app

# Dependencies first, as their own layer: package.json changes far less often than
# the source, so an edit to a component does not re-run `npm ci`.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

# The OpenAPI schema is a build input, not a runtime one -- the generated types are
# erased by the compiler. Copied from the repository root, which is why the build
# context is `.` and not `./frontend`.
COPY docs/api/openapi.json ../docs/api/openapi.json
COPY frontend/ ./

# VITE_API_BASE_URL is left unset on purpose, so the bundle calls /api and nginx
# proxies it. Baking an absolute API URL into the image would make the image
# environment-specific, and Vite offers no way to change it afterwards.
RUN npm run build

# 1.31, not 1.27. Trivy found 2 CRITICAL and 33 HIGH in the 1.27-alpine base --
# openssl 3.3.3-r0 and libxml2 2.13.4-r5, both with published fixes. Those are not
# nginx bugs; they are an Alpine layer that stopped being rebuilt.
#
# 1.29 was measured too and still carried 11 fixable HIGH. 1.31 measures at **zero**
# fixable HIGH or CRITICAL, which is why the security gate can hold this image to
# zero rather than to a tolerance.
#
# Pinned to a minor version rather than floating `alpine`, so a base bump is a
# commit someone reviewed rather than something that changes under a rebuild -- and
# so the number above stays checkable.
FROM nginx:1.31-alpine AS runtime

COPY docker/frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

# nginx's own image already runs the master as root and workers as nginx; this
# image serves static files and terminates nothing, so it needs no more than that.
EXPOSE 5173

HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
    CMD wget -qO- http://localhost:5173/ >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
