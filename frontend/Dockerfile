# syntax=docker/dockerfile:1
FROM node:22-alpine AS build
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# Browser requests always use the same-origin reverse proxy; no model keys.
ENV VITE_API_BASE_URL=/api
RUN npm run build

FROM nginx:stable-alpine
COPY deploy/nginx.conf /etc/nginx/nginx.conf
COPY --from=build /web/dist /usr/share/nginx/html
USER nginx
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
