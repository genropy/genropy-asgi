#!/bin/bash
# Photograph the local test database into the lab's init scripts. Run from
# benchmarks/docker/ whenever the reference data should be refreshed; the
# dump is runtime material, never committed.
set -e
mkdir -p runtime/initdb
pg_dump -d test_invoice_pg --no-owner --no-privileges > runtime/initdb/10_test_invoice_pg.sql
cat > runtime/initdb/20_bridge_copy.sql <<'SQL'
CREATE DATABASE test_invoice_bridge TEMPLATE test_invoice_pg;
SQL
echo "dump: $(du -h runtime/initdb/10_test_invoice_pg.sql | cut -f1)"
