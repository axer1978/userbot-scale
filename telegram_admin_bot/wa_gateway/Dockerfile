# Stage 1: compile TypeScript with the full (dev) dependency set.
FROM node:24-slim AS build
WORKDIR /app
ENV NPM_CONFIG_UPDATE_NOTIFIER=false NPM_CONFIG_FUND=false
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json ./
COPY src ./src
RUN npm run build

# Stage 2: runtime with production dependencies only, as the unprivileged
# `node` user. No secrets are baked in: DATABASE_URL, REDIS_URL and
# USERBOT_MASTER_KEY come from docker-compose.yml / .env at run time.
# No ports: the gateway only dials out (Postgres, Valkey, WhatsApp).
FROM node:24-slim
WORKDIR /app
ENV NODE_ENV=production NPM_CONFIG_UPDATE_NOTIFIER=false NPM_CONFIG_FUND=false
COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY --from=build /app/dist ./dist
USER node
CMD ["node", "dist/main.js"]
