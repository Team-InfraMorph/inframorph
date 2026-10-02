FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY prisma ./prisma
RUN DATABASE_URL=postgresql://localhost/unused ./node_modules/.bin/prisma validate \
    && ./node_modules/.bin/prisma generate
COPY src ./src
RUN mkdir -p uploads && chown node:node uploads
USER node
EXPOSE 3000
CMD ["node", "src/server.js"]
