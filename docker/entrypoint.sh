#!/bin/sh
# Custom entrypoint: copies baked-in app code to /conf/apps/ (writable emptyDir),
# then delegates to the upstream AppDaemon entrypoint.

set -e

STAGED=/opt/appdaemon-code
CONF=/conf

# Copy app code from image to writable /conf/apps/
mkdir -p "$CONF/apps"
cp -r "$STAGED/apps/"* "$CONF/apps/"

# Copy providers into apps/providers/ (AppDaemon sys.path = /conf/apps)
cp -r "$STAGED/providers" "$CONF/apps/providers"

echo "[entrypoint] App code deployed from image to $CONF/apps/"

# Delegate to the upstream AppDaemon entrypoint: /start.sh from 4.5.16
# (upstream moved it), /usr/src/app/dockerStart.sh before that.
for upstream in /start.sh /usr/src/app/dockerStart.sh; do
  if [ -x "$upstream" ]; then
    exec "$upstream" "$@"
  elif [ -f "$upstream" ]; then
    exec /bin/sh "$upstream" "$@"
  fi
done
echo "[entrypoint] no upstream AppDaemon start script found" >&2
exit 1
