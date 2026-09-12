# syntax=docker/dockerfile:1
FROM oven/bun:1.3.14 AS deps
WORKDIR /app
COPY package.json bun.lock ./
RUN bun install --frozen-lockfile

FROM oven/bun:1.3.14 AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# next build inlines this value, which carries the /api prefix; changing the proxy's mount point is a rebuild, not a restart.
ARG NEXT_PUBLIC_ENGINE_URL
ENV NEXT_PUBLIC_ENGINE_URL=$NEXT_PUBLIC_ENGINE_URL
RUN bun run build

FROM oven/bun:1.3.14 AS proddeps
WORKDIR /app
COPY package.json bun.lock ./
RUN bun install --frozen-lockfile --production

FROM node:22-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production
COPY --from=proddeps /app/node_modules ./node_modules
COPY --from=build /app/.next ./.next
COPY package.json next.config.ts next-env.d.ts ./
EXPOSE 3000
CMD ["node", "node_modules/next/dist/bin/next", "start", "-H", "0.0.0.0", "-p", "3000"]
