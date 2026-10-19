#!/bin/sh
# Private CA and server certificate for the kernel's TLS listener (ADR-025).
# Usage: make-tls.sh <cardea-private-ip> <output-dir>. Rotate yearly; the service
# reads certificates at startup only.
set -eu
ip="$1"; out="$2"
umask 077
mkdir -p "$out"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 1825 \
  -subj "/CN=cardea private CA" -keyout "$out/ca.key" -out "$out/ca.crt"
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
  -subj "/CN=cardea kernel" -keyout "$out/server.key" -out "$out/server.csr"
printf 'subjectAltName=IP:%s\nextendedKeyUsage=serverAuth\n' "$ip" > "$out/ext.cnf"
openssl x509 -req -in "$out/server.csr" -CA "$out/ca.crt" -CAkey "$out/ca.key" \
  -CAcreateserial -days 365 -extfile "$out/ext.cnf" -out "$out/server.crt"
rm -f "$out/server.csr" "$out/ext.cnf"
echo "Copy $out/ca.crt to the Telos host as cardea-ca.crt; keep ca.key offline."
