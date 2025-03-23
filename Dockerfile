# Use the Kubeflow Code-Server Python image
FROM kubeflownotebookswg/codeserver-python:latest

# Switch to root to make modifications
USER root

# Remove code-server completely
RUN apt-get remove -y code-server \
    && apt-get autoremove -y \
    && apt-get clean \
    && rm -rf /etc/services.d/code-server \
    && rm -rf /usr/lib/code-server \
    && rm -rf /usr/bin/code-server \
    && rm -rf ${HOME}/.local/share/code-server \
    && rm -rf ${HOME_TMP}/.local/share/code-server

# Install system dependencies including Nginx
RUN apt-get update && apt-get install -y \
    git \
    curl \
    wget \
    build-essential \
    python3-dev \
    libgl1-mesa-glx \
    ffmpeg \
    nginx \
    gettext-base \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Open WebUI
RUN pip install --upgrade pip && \
    pip install open-webui && \
    # Fix permissions for static files
    chmod -R 777 /opt/conda/lib/python3.11/site-packages/open_webui/static

# Create data directory
RUN mkdir -p /tmp_home/jovyan/.open-webui
RUN chown -R ${NB_USER}:${NB_GID} /tmp_home/jovyan/.open-webui

# Configure Nginx
RUN rm -f /etc/nginx/sites-enabled/default
COPY nginx.conf /etc/nginx/conf.d/openwebui.conf

# Create service directories
RUN mkdir -p /etc/services.d/openwebui
RUN mkdir -p /etc/services.d/nginx

# Copy the run scripts for services
COPY openwebui-run /etc/services.d/openwebui/run
COPY nginx-run /etc/services.d/nginx/run
RUN chmod 755 /etc/services.d/openwebui/run
RUN chmod 755 /etc/services.d/nginx/run

# Expose port 8888
EXPOSE 8888

# Stay as root (Don't switch back to non-root user)
# ENTRYPOINT will still use root

# Keep the original entrypoint
ENTRYPOINT ["/init"]
