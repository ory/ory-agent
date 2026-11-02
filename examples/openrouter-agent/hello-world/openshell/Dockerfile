# Credential-free runtime image. OpenShell injects provider placeholders at sandbox start.
FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force
COPY --chmod=0444 agent.ts ./

# OpenShell grants /app read-only and /tmp read-write; keep Argus state in /tmp.
ENV NODE_ENV=production \
    XDG_CONFIG_HOME=/tmp/ory-agent
USER node
