FROM nginx:alpine
COPY app /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY inject-config.sh /docker-entrypoint.d/40-inject-config.sh
RUN chmod +x /docker-entrypoint.d/40-inject-config.sh
EXPOSE 80
