FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
    && groupadd --gid 10001 fns \
    && useradd --uid 10001 --gid 10001 --no-create-home fns
COPY export_nalog_receipts.py .
COPY fns_mcp ./fns_mcp
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "fns_mcp.server"]
