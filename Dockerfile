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

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    wget \
    build-essential \
    python3-dev \
    libgl1-mesa-glx \
    ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install additional packages for the proxy server
RUN pip install aiohttp

# Copy the proxy server script
COPY proxy_server.py /tmp_home/jovyan/
RUN chown ${NB_USER}:${NB_GID} /tmp_home/jovyan/proxy_server.py

# Create openwebui service directory
RUN mkdir -p /etc/services.d/openwebui

# Copy the run script for the Open WebUI service
COPY openwebui-run /etc/services.d/openwebui/run
RUN chmod 755 /etc/services.d/openwebui/run && \
    chown ${NB_USER}:${NB_GID} /etc/services.d/openwebui/run

# Create directories and set permissions
RUN mkdir -p /tmp_home/jovyan/.open-webui && \
    chown -R ${NB_USER}:${NB_GID} /tmp_home/jovyan/.open-webui

# Switch to non-root user for pip install
USER $NB_UID

# Install Open WebUI as non-root user
RUN pip install --user open-webui

# Environment variables set through proxy server

# Expose port 8888
EXPOSE 8888

# Keep the original entrypoint
USER root
ENTRYPOINT ["/init"]
