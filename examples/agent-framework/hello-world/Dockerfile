# Credential-free runtime image. Azure Container Apps injects runtime values from app secrets.
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

WORKDIR /app
COPY requirements.txt requirements-adapter.txt ./
RUN python -m pip install --no-cache-dir --requirement requirements.txt \
    && python -m pip install --no-cache-dir --no-deps --requirement requirements-adapter.txt
# ACR Tasks builds without BuildKit, so COPY --chmod is unavailable. Root-owned 0644 files are
# already read-only for the nobody runtime user.
COPY agent.py app.py ./

# Keep Argus state outside the read-only application directory.
ENV PYTHONUNBUFFERED=1 \
    PORT=3978 \
    XDG_CONFIG_HOME=/tmp/ory-agent
EXPOSE 3978
USER nobody
CMD ["python", "app.py"]
