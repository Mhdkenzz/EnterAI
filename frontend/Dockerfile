FROM node:22-alpine
WORKDIR /app

RUN apk add --no-cache curl

# `npm ci` installs exactly the lockfile, so an image build cannot silently pick up
# a different dependency tree than the one that was tested.
COPY package*.json ./
RUN npm ci

COPY . .
ARG BUILD_ENVIRONMENT=development
ARG NEXT_PUBLIC_API_URL=""
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
# Next public variables are compiled into the browser bundle.  Hosted builds must
# receive the public API endpoint now; a runtime Compose environment cannot fix it.
RUN if [ "$BUILD_ENVIRONMENT" = "staging" ] || [ "$BUILD_ENVIRONMENT" = "production" ]; then \
      test -n "$NEXT_PUBLIC_API_URL" && \
      case "$NEXT_PUBLIC_API_URL" in https://*) ;; *) echo 'NEXT_PUBLIC_API_URL must be an HTTPS URL for hosted builds' >&2; exit 1;; esac; \
    fi && npm run build

# node:alpine ships an unprivileged `node` user; the build runs as root so it can
# write node_modules and .next, then hands the built app over to that user.
RUN chown -R node:node /app
USER node

EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:3000 || exit 1

CMD ["npm", "run", "start"]
