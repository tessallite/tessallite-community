# Stage 1: build React app
FROM node:20-alpine AS builder

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci

COPY frontend/ .
COPY shared-ui/ /app/shared-ui/

# shared-ui is a source-only package with no node_modules. tsc follows path
# aliases into its source and needs to resolve react/zustand/MUI types.
# Symlinking node_modules one level up simulates npm-workspace hoisting so
# Node module resolution from /app/shared-ui/ walks up and finds the deps.
RUN ln -s /app/frontend/node_modules /app/node_modules

# public/help is a symlink locally pointing at ../../help. Docker copies the
# symlink itself, not the target, so we replace it with a real copy from the
# repo-root help/ tree before building.
RUN rm -rf public/help
COPY help/ public/help/

# Build-time API URL overrides. Leave empty by default so the SPA uses
# same-origin relative paths (routed by the nginx proxy in stage 2).
# Set these only when you need to bypass the proxy (e.g. for a dev build
# pointed at a remote backend).
ARG VITE_API_BASE_URL=
ARG VITE_SCHEDULER_URL=
ARG VITE_OPTIMIZER_URL=
ARG VITE_QUERY_ROUTER_URL=
ARG VITE_ENABLE_DEMO_LOGIN=
ARG VITE_DEMO_TENANT_SLUG=
ARG VITE_DEMO_EMAIL=
ARG VITE_DEMO_PASSWORD=
# Gateway connection strings shown in the Endpoints panel.
# Must be set at build time via --build-arg so Vite bakes them into the bundle.
# Without these, EndpointsPanel falls back to window.location.hostname, which is
# the frontend domain (cloud.tessallite.io), not the gateway domain (sql.cloud.tessallite.io).
ARG VITE_GATEWAY_URL=
ARG VITE_GATEWAY_JDBC_HOST=
ARG VITE_GATEWAY_JDBC_PORT=
# Model service base URL for the MCP endpoint config shown to users.
# In GCP the model service is nginx-proxied at the app domain (no port).
# Locally empty — component falls back to http://localhost:<port>.
ARG VITE_MODEL_SERVICE_URL=
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
ENV VITE_SCHEDULER_URL=$VITE_SCHEDULER_URL
ENV VITE_OPTIMIZER_URL=$VITE_OPTIMIZER_URL
ENV VITE_QUERY_ROUTER_URL=$VITE_QUERY_ROUTER_URL
ENV VITE_ENABLE_DEMO_LOGIN=$VITE_ENABLE_DEMO_LOGIN
ENV VITE_DEMO_TENANT_SLUG=$VITE_DEMO_TENANT_SLUG
ENV VITE_DEMO_EMAIL=$VITE_DEMO_EMAIL
ENV VITE_DEMO_PASSWORD=$VITE_DEMO_PASSWORD
ENV VITE_GATEWAY_URL=$VITE_GATEWAY_URL
ENV VITE_GATEWAY_JDBC_HOST=$VITE_GATEWAY_JDBC_HOST
ENV VITE_GATEWAY_JDBC_PORT=$VITE_GATEWAY_JDBC_PORT
ENV VITE_MODEL_SERVICE_URL=$VITE_MODEL_SERVICE_URL

RUN npm run build

# Stage 2: serve with nginx (same-origin API proxy)
FROM nginx:1.25-alpine

RUN mkdir -p /etc/nginx/certs

COPY --from=builder /app/frontend/dist /usr/share/nginx/html
COPY excel-plugin/dist /usr/share/nginx/html/excel-plugin

# The official nginx image processes files under /etc/nginx/templates/
# through envsubst at container start-up. Restrict substitution to our
# upstream URL vars so nginx's own $host / $remote_addr etc. are left
# alone.
COPY frontend/nginx.conf.template /etc/nginx/templates/default.conf.template
ENV NGINX_ENVSUBST_TEMPLATE_SUFFIX=.template
ENV NGINX_ENVSUBST_OUTPUT_DIR=/etc/nginx/conf.d
ENV NGINX_ENVSUBST_FILTER='^(LISTEN_PORT|RESOLVER|DISABLE_HTTPS_REDIRECT|TRUST_PROXY_HEADERS|MODEL_SERVICE_URL|MODEL_SERVICE_HOST|QUERY_ROUTER_URL|QUERY_ROUTER_HOST|OPTIMIZER_URL|OPTIMIZER_HOST|SCHEDULER_URL|SCHEDULER_HOST|GATEWAY_URL|GATEWAY_HOST|AGENT_SERVICE_URL|AGENT_SERVICE_HOST)$'

# Sensible defaults for compose (overridden in Cloud Run / on-prem).
# Cloud Run injects $PORT — the runtime env sets LISTEN_PORT=$PORT there.
ENV LISTEN_PORT=3000
ENV DISABLE_HTTPS_REDIRECT=
ENV TRUST_PROXY_HEADERS=
ENV RESOLVER=127.0.0.11
ENV MODEL_SERVICE_URL=http://model-service:8001
ENV MODEL_SERVICE_HOST=model-service
ENV QUERY_ROUTER_URL=http://query-router:8000
ENV QUERY_ROUTER_HOST=query-router
ENV OPTIMIZER_URL=http://optimizer:8000
ENV OPTIMIZER_HOST=optimizer
ENV SCHEDULER_URL=http://scheduler:8000
ENV SCHEDULER_HOST=scheduler
ENV GATEWAY_URL=http://gateway:8080
ENV GATEWAY_HOST=gateway
ENV AGENT_SERVICE_URL=http://agent-service:8000
ENV AGENT_SERVICE_HOST=agent-service

RUN chown -R nginx:nginx /usr/share/nginx/html /var/cache/nginx /var/log/nginx /etc/nginx/conf.d && \
    chmod -R 755 /usr/share/nginx/html && \
    touch /var/run/nginx.pid && chown nginx:nginx /var/run/nginx.pid
USER nginx

EXPOSE 3000 3443

CMD ["nginx", "-g", "daemon off;"]
