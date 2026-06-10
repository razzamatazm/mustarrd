FROM ghcr.io/linuxserver/baseimage-ubuntu:noble AS comskip-builder

WORKDIR /tmp/comskip

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    yasm \
    pkg-config \
    autoconf \
    automake \
    libtool \
    libargtable2-dev \
    libavcodec-dev \
    libavformat-dev \
    libavutil-dev \
    libswscale-dev \
    libpostproc-dev \
  && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch fancybits https://github.com/fancybits/Comskip . \
  && ./autogen.sh \
  && ./configure \
  && make

FROM --platform=$BUILDPLATFORM node:20-alpine AS frontend-builder

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend ./
RUN npm run build

FROM ghcr.io/linuxserver/baseimage-ubuntu:noble

WORKDIR /app/backend

# Runtime deps + jellyfin-ffmpeg (bundles nvenc/qsv/vaapi/amf builds)
RUN sed -i 's|http://ports.ubuntu.com/ubuntu-ports|https://ports.ubuntu.com/ubuntu-ports|g' /etc/apt/sources.list \
  && sed -i 's|http://archive.ubuntu.com/ubuntu|https://archive.ubuntu.com/ubuntu|g' /etc/apt/sources.list \
  && apt-get update -o Acquire::Retries=3 \
  && apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    python3-pip \
    ca-certificates \
    curl \
    gnupg \
    gosu \
    libargtable2-0 \
    libavcodec60 \
    libavformat60 \
    libavutil58 \
    libswscale7 \
    libpostproc57 \
  && install -d -m 0755 /etc/apt/keyrings \
  && curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/jellyfin.gpg \
  && . /etc/os-release \
  && echo "deb [signed-by=/etc/apt/keyrings/jellyfin.gpg] https://repo.jellyfin.org/${ID} ${VERSION_CODENAME} main" \
       > /etc/apt/sources.list.d/jellyfin.list \
  && apt-get update -o Acquire::Retries=3 \
  && apt-get install -y --no-install-recommends jellyfin-ffmpeg7 \
  && if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
       for pkg in mesa-va-drivers intel-media-va-driver i965-va-driver; do \
         apt-get install -y --no-install-recommends -o Acquire::Retries=3 "$pkg" || true; \
       done; \
     fi \
  && rm -rf /var/lib/apt/lists/*

ENV \
    CATCHUP_FFMPEG_PATH="/usr/lib/jellyfin-ffmpeg/ffmpeg" \
    CATCHUP_FFPROBE_PATH="/usr/lib/jellyfin-ffmpeg/ffprobe" \
    CATCHUP_FRONTEND_DIST="/app/frontend/dist" \
    LIBVA_DRIVERS_PATH="/usr/lib/jellyfin-ffmpeg/lib/dri:/usr/lib/x86_64-linux-gnu/dri:/usr/lib/aarch64-linux-gnu/dri" \
    LD_LIBRARY_PATH="/usr/lib/jellyfin-ffmpeg/lib"

# Install dependencies in a venv (PEP 668 safe)
ENV VENV_PATH="/opt/venv"
RUN python3 -m venv "$VENV_PATH"
ENV PATH="$VENV_PATH/bin:$PATH"
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application + tools
COPY backend /app/backend
COPY --from=frontend-builder /app/frontend/dist /app/frontend/dist
COPY backend/ffmpegwrapper.sh /usr/local/bin/ffmpegwrapper.sh
COPY scripts /app/scripts
COPY backend/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY tools /app/tools
COPY comskip.ini /app/comskip.ini

# Copy comskip binary from builder
COPY --from=comskip-builder /tmp/comskip/comskip /usr/local/bin/comskip
RUN chmod +x /usr/local/bin/comskip

# Symlink jellyfin-ffmpeg so callers expecting /usr/local/bin/ffmpeg keep working
RUN ln -sf /usr/lib/jellyfin-ffmpeg/ffmpeg /usr/local/bin/ffmpeg \
  && ln -sf /usr/lib/jellyfin-ffmpeg/ffprobe /usr/local/bin/ffprobe

# Ensure repo-provided tools are executable if present
RUN chmod +x /app/tools/bin/*/ffmpeg /app/tools/bin/*/ffprobe /app/tools/bin/*/comskip 2>/dev/null || true
RUN chmod +x /app/scripts/qa/*.sh 2>/dev/null || true
RUN chmod +x /usr/local/bin/ffmpegwrapper.sh

# Create data directories
RUN mkdir -p /app/downloads /app/completed /app/config \
  && chmod +x /usr/local/bin/docker-entrypoint.sh

# Expose port
EXPOSE 4177

# Run the application
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "4177", "--no-access-log"]
