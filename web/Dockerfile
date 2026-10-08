# syntax=docker/dockerfile:1
FROM node:22-slim AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
ARG VITE_API_BASE_URL=http://127.0.0.1:8000
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

# nginx serves the compiled static files. A reverse proxy (TLS, routing)
# is expected in front of this container in production.
FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
# SPA: any path that is not a real file falls back to index.html so
# client-side routing (react-router) works on a direct refresh.
# Defense-in-depth headers. No CSP yet on purpose: a correct CSP depends on
# the configurable API origin (VITE_API_BASE_URL) and the production reverse
# proxy; writing one without that context risks breaking the app.
RUN printf 'server {\n\
    listen 80;\n\
    root /usr/share/nginx/html;\n\
    add_header X-Frame-Options "DENY" always;\n\
    add_header X-Content-Type-Options "nosniff" always;\n\
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;\n\
    location / {\n\
        try_files $uri /index.html;\n\
    }\n\
}\n' > /etc/nginx/conf.d/default.conf
