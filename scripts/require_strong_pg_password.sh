#!/bin/sh
# Postgres 容器入口包装：拒绝空密码、短密码和已知占位符。
# 通过后再交给官方镜像入口，以保留初始化脚本和 healthcheck。
set -eu

pw="${POSTGRES_PASSWORD:-}"
case "$pw" in
  ""|changeme|change_me|change-me|password|your_password|your_pg_password|admin|postgres|agent_user)
    echo "POSTGRES_PASSWORD is missing or a known placeholder. Set PG_PASSWORD to a strong value of at least 12 characters." >&2
    exit 1
    ;;
esac

if [ "${#pw}" -lt 12 ]; then
  echo "POSTGRES_PASSWORD must be at least 12 characters." >&2
  exit 1
fi

exec docker-entrypoint.sh postgres
