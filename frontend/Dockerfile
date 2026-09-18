FROM node:22-alpine
WORKDIR /app

RUN apk add --no-cache curl

# `npm ci` installs exactly the lockfile, so an image build cannot silently pick up
# a different dependency tree than the one that was tested.
COPY package*.json ./
RUN npm ci

COPY . .
RUN npm run build

# node:alpine ships an unprivileged `node` user; the build runs as root so it can
# write node_modules and .next, then hands the built app over to that user.
RUN chown -R node:node /app
USER node

EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:3000 || exit 1

CMD ["npm", "run", "start"]
