#!/usr/bin/with-contenv bash
cd /home/jovyan
exec 2>&1

# Create a runtime config by substituting variables
cat /etc/nginx/conf.d/openwebui.conf | NB_PREFIX=${NB_PREFIX:-/} envsubst '${NB_PREFIX}' > /etc/nginx/conf.d/default.conf

# Remove the template to avoid confusion
rm /etc/nginx/conf.d/openwebui.conf

# Run Nginx in the foreground
exec nginx -g 'daemon off;'
