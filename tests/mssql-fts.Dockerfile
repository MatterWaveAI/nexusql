# SQL Server 2022 with the full-text search component.
#
# The stock image leaves full-text out, and a container cannot be given it
# from outside once it is running (CI service containers in particular), so
# the suite's SQL Server comes from this image everywhere: CI, the compose
# file and scripts/test_databases.py. Without it the full-text tests skip.
FROM mcr.microsoft.com/mssql/server:2022-latest

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
      | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
 && . /etc/os-release \
 && echo "deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/ubuntu/${VERSION_ID}/mssql-server-2022 ${VERSION_CODENAME} main" \
      > /etc/apt/sources.list.d/mssql-server-2022.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends mssql-server-fts \
 && rm -rf /var/lib/apt/lists/*
USER mssql
