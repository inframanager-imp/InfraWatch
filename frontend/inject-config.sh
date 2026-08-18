#!/bin/sh
set -e
API_BASE="${API_BASE:-http://localhost:8000}"
sed "s|__API_BASE__|${API_BASE}|g" /usr/share/nginx/html/config.js.template > /usr/share/nginx/html/config.js
