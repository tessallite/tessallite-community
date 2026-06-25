#!/bin/sh
# Ensure nginx has a TLS cert before it starts.
#
# nginx.conf.template references /etc/nginx/certs/localhost.{crt,key}
# unconditionally (listen 3443 ssl). Without the files nginx aborts with
# "cannot load certificate ... No such file" and the container crashloops.
#
# Dev/compose stacks bind-mount a host cert and Kubernetes can mount one from a
# Secret; in BOTH those cases the files already exist and this script is a no-op.
# On a fresh install with no cert provided (community compose / Helm default) it
# generates a self-signed default so the UI comes up. Operators front the service
# with their own TLS (ingress / proxy / mounted Secret), which overrides this.
set -e
CERT_DIR=/etc/nginx/certs
if [ ! -f "$CERT_DIR/localhost.crt" ] || [ ! -f "$CERT_DIR/localhost.key" ]; then
  echo "ensure-cert: no TLS cert provided — generating a self-signed default"
  mkdir -p "$CERT_DIR"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$CERT_DIR/localhost.key" -out "$CERT_DIR/localhost.crt" \
    -subj "/CN=localhost" >/dev/null 2>&1
fi
