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

# Delegate to the upstream AppDaemon entrypoint
exec /usr/src/app/dockerStart.sh "$@"
