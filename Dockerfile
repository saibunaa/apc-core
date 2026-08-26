FROM python:3.12-alpine
WORKDIR /app
COPY apc_core /app/apc_core
ENV APC_CORE_ARTIFACT_ROOT=/state
EXPOSE 8769
CMD ["python3", "-m", "apc_core.server", "--host", "0.0.0.0", "--container-ingress", "--port", "8769", "--manifest", "/state/accepted_snapshot.json"]
