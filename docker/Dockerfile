# Custom AppDaemon image with app code and dependencies baked in.
# Built on merge to main; pushed to ghcr.io/thaynes43/appdaemon.
#
# Runtime volumes (from Kubernetes):
#   /conf          — emptyDir (writable; entrypoint populates apps/ from image)
#   /conf/appdaemon.yaml — Secret subPath mount
#   /conf/secrets.yaml   — Secret subPath mount
#   /media         — NFS mount for media files

FROM acockburn/appdaemon:latest

# --- Build-time: install app dependencies ---
# Only runtime deps; dev deps (pytest, ruamel.yaml, etc.) are not needed.
COPY docker/requirements-prod.txt /tmp/requirements-prod.txt
RUN pip install --no-cache-dir -r /tmp/requirements-prod.txt && rm /tmp/requirements-prod.txt

# --- Build-time: process apps-prod.yaml → apps.yaml ---
# Strips `disable: true` and `debug_preserve_run_dirs: true` from all app configs.
COPY docker/process-apps-yaml.py /tmp/process-apps-yaml.py
COPY appdaemon/apps/apps-prod.yaml /tmp/apps-prod.yaml
RUN python3 /tmp/process-apps-yaml.py /tmp/apps-prod.yaml /tmp/apps.yaml \
    && rm /tmp/process-apps-yaml.py /tmp/apps-prod.yaml

# --- Stage app code at /opt/appdaemon-code (read-only in image) ---
# Entrypoint copies this to /conf/apps/ at runtime (writable emptyDir).
COPY appdaemon/apps/ /opt/appdaemon-code/apps/
COPY appdaemon/providers/ /opt/appdaemon-code/providers/

# Replace apps-prod.yaml with the processed apps.yaml; remove dev-only files
RUN rm -f /opt/appdaemon-code/apps/apps-prod.yaml \
          /opt/appdaemon-code/apps/apps-dev.yaml \
    && mv /tmp/apps.yaml /opt/appdaemon-code/apps/apps.yaml

# --- Custom entrypoint ---
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
