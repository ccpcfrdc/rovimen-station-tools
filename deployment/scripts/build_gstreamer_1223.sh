#!/bin/bash
#
# GStreamer 1.22.3 — Build from source
#
# Builds GStreamer 1.22.3 and installs it to /opt/gst-1.22.
# Does NOT activate it for RMS — see "Activation" section at the bottom.
#
# Tested on: Ubuntu 22.04 (gmn0003/gmn0002), Intel x86_64
# Build time: ~20-30 min depending on CPU
#
# Usage:
#   Run as gmn user (not root). sudo will be invoked for apt/install steps.
#   Recommend running inside a tmux session so it survives SSH drops:
#
#     tmux new -s gst_build
#     bash /home/gmn/rovimen_scripts/build_gstreamer_1223.sh 2>&1 | tee /home/gmn/gst_build.log
#
# Idempotent: safe to re-run. Skips clone/configure if already done.
# To force reconfigure: rm -rf /home/gmn/gstreamer-1.22.3/build
#
# Activation (after build + testing):
#   Add these env vars to /etc/systemd/system/rms-cam1.service and rms-cam2.service,
#   under [Service]:
#
#     Environment=GST_PLUGIN_PATH=/opt/gst-1.22/lib/x86_64-linux-gnu/gstreamer-1.0
#     Environment=LD_LIBRARY_PATH=/opt/gst-1.22/lib/x86_64-linux-gnu
#     Environment=GI_TYPELIB_PATH=/opt/gst-1.22/lib/x86_64-linux-gnu/girepository-1.0
#
#   Then: sudo systemctl daemon-reload && sudo systemctl restart rms-cam1 rms-cam2
#

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GST_VERSION="1.22.3"
GST_TAG="1.22.3"
SRC_DIR="/home/gmn/gstreamer-1.22.3"
BUILD_DIR="${SRC_DIR}/build"
INSTALL_PREFIX="/opt/gst-1.22"
REPO_URL="https://gitlab.freedesktop.org/gstreamer/gstreamer.git"
ARCH="x86_64-linux-gnu"

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${GREEN}[GST-BUILD]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
die()  { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
[[ "$EUID" -eq 0 ]] && die "Do not run as root. Run as gmn — sudo will be used where needed."
command -v sudo  >/dev/null || die "sudo not found"
command -v git   >/dev/null || die "git not found — install with: sudo apt install git"

log "GStreamer ${GST_VERSION} build script starting"
log "  Source : ${SRC_DIR}"
log "  Install: ${INSTALL_PREFIX}"

# ---------------------------------------------------------------------------
# Step 1: System dependencies
# ---------------------------------------------------------------------------
log "Installing system dependencies..."

# Core build tools
BUILD_DEPS=(
    build-essential
    ninja-build
    meson
    pkg-config
    git
    bison
    flex
    gettext
    python3-pip        # meson may need it on some systems
)

# GLib / GObject / introspection
GLIB_DEPS=(
    libglib2.0-dev
    libgirepository1.0-dev   # REQUIRED for Python GI typelibs (gi.repository.Gst)
    gir1.2-glib-2.0
)

# GStreamer-specific libraries (needed to avoid bundled subprojects)
GST_DEPS=(
    libxml2-dev          # gst-plugins-bad: dashdemux, mssdemux
    libpango1.0-dev      # gst-plugins-base: pango text overlay
    libharfbuzz-dev      # pango dependency
    libjson-glib-dev     # gst-plugins-bad: json parsing
    libgraphene-1.0-dev  # gst-plugins-bad: GL/vulkan
    libsoup-3.0-dev      # gst-plugins-bad: http/dash/hls
    libssl-dev           # RTSP/SRTP/HTTPS support
    liborc-0.4-dev       # SIMD optimization for video processing
)

# Codec libraries
CODEC_DEPS=(
    libmp3lame-dev       # uglyplugins: MAD mp3
    libfdk-aac-dev       # uglyplugins: AAC
    libopus-dev          # good plugins: opus
    libvpx-dev           # good plugins: vp8/vp9
    libflac-dev          # good plugins: flac
    libwavpack-dev       # good plugins: wavpack
    libspeex-dev         # good plugins: speex
    libx264-dev          # bad plugins: x264enc (H.264)
    libva-dev            # vaapi: VA-API hardware acceleration
    libva-drm2           # vaapi: DRM backend
)

# libav (ffmpeg) for gst-libav
LIBAV_DEPS=(
    libavcodec-dev
    libavformat-dev
    libavutil-dev
    libswscale-dev
    libswresample-dev
)

ALL_DEPS=(
    "${BUILD_DEPS[@]}"
    "${GLIB_DEPS[@]}"
    "${GST_DEPS[@]}"
    "${CODEC_DEPS[@]}"
    "${LIBAV_DEPS[@]}"
)

sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends "${ALL_DEPS[@]}" \
    || warn "Some packages failed to install — continuing (may be unavailable on this OS version)"

log "System dependencies installed."

# ---------------------------------------------------------------------------
# Step 2: Clone source
# ---------------------------------------------------------------------------
if [[ -d "${SRC_DIR}/.git" ]]; then
    log "Source already cloned at ${SRC_DIR} — skipping clone."
else
    log "Cloning GStreamer ${GST_TAG} (shallow)..."
    git clone --depth=1 --branch "${GST_TAG}" "${REPO_URL}" "${SRC_DIR}"
    log "Clone done."
fi

cd "${SRC_DIR}"

# ---------------------------------------------------------------------------
# Step 3: Meson configure
# ---------------------------------------------------------------------------
if [[ -d "${BUILD_DIR}" ]]; then
    log "Build directory exists — skipping meson setup. (rm -rf ${BUILD_DIR} to reconfigure)"
else
    log "Running meson setup..."
    meson setup "${BUILD_DIR}" \
        --prefix="${INSTALL_PREFIX}" \
        --libdir="lib/${ARCH}" \
        --wrap-mode=nofallback \
        \
        -Dintrospection=enabled \
        \
        -Dbase=enabled \
        -Dgood=enabled \
        -Dugly=enabled \
        -Dbad=enabled \
        -Dlibav=enabled \
        -Dvaapi=enabled \
        \
        -Ddoc=disabled \
        -Dtests=disabled \
        -Dexamples=disabled \
        -Dpython=disabled \
        -Dges=disabled \
        -Dgst-rtsp-server:enabled=disabled \
        \
        -Dgst-plugins-ugly:enabled=enabled \
        -Dgst-plugins-ugly:a52dec=disabled \
        \
        -Dgst-plugins-bad:enabled=enabled \
        -Dgst-plugins-bad:nvcodec=disabled \
        -Dgst-plugins-bad:webrtc=disabled \
        -Dgst-plugins-bad:vulkan=disabled

    log "Meson configure done."
fi

# ---------------------------------------------------------------------------
# Step 4: Build
# ---------------------------------------------------------------------------
log "Building with ninja ($(nproc) jobs) — this takes 20-30 minutes..."
ninja -C "${BUILD_DIR}" -j"$(nproc)"
log "Build complete."

# ---------------------------------------------------------------------------
# Step 5: Install
# ---------------------------------------------------------------------------
log "Installing to ${INSTALL_PREFIX}..."
sudo mkdir -p "${INSTALL_PREFIX}"
sudo ninja -C "${BUILD_DIR}" install
log "Install complete."

# ---------------------------------------------------------------------------
# Step 6: Verify
# ---------------------------------------------------------------------------
log "Verifying installation..."

GST_BIN="${INSTALL_PREFIX}/bin/gst-launch-1.0"
if [[ ! -f "${GST_BIN}" ]]; then
    die "gst-launch-1.0 not found at ${GST_BIN} — install may have failed"
fi

GST_INSTALLED_VERSION=$(
    GST_PLUGIN_PATH="${INSTALL_PREFIX}/lib/${ARCH}/gstreamer-1.0" \
    LD_LIBRARY_PATH="${INSTALL_PREFIX}/lib/${ARCH}" \
    "${GST_BIN}" --version | head -1
)
log "Installed: ${GST_INSTALLED_VERSION}"

TYPELIB_DIR="${INSTALL_PREFIX}/lib/${ARCH}/girepository-1.0"
TYPELIB_COUNT=$(ls "${TYPELIB_DIR}"/*.typelib 2>/dev/null | wc -l)
if [[ "${TYPELIB_COUNT}" -eq 0 ]]; then
    die "No typelibs found in ${TYPELIB_DIR} — introspection may not have built. Check meson log."
fi
log "GI typelibs: ${TYPELIB_COUNT} found in ${TYPELIB_DIR}"

# Quick Python GI test
GI_TEST=$(
    GST_PLUGIN_PATH="${INSTALL_PREFIX}/lib/${ARCH}/gstreamer-1.0" \
    LD_LIBRARY_PATH="${INSTALL_PREFIX}/lib/${ARCH}" \
    GI_TYPELIB_PATH="${TYPELIB_DIR}" \
    python3 -c "
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
Gst.init(None)
print('Python GI OK:', Gst.version_string())
" 2>&1
)
log "${GI_TEST}"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
log "============================================================"
log " GStreamer ${GST_VERSION} build COMPLETE"
log "============================================================"
echo ""
echo "  Install path : ${INSTALL_PREFIX}"
echo "  Plugins      : ${INSTALL_PREFIX}/lib/${ARCH}/gstreamer-1.0/"
echo "  Typelibs     : ${TYPELIB_DIR}"
echo ""
echo "  To test manually:"
echo "    export GST_PLUGIN_PATH=${INSTALL_PREFIX}/lib/${ARCH}/gstreamer-1.0"
echo "    export LD_LIBRARY_PATH=${INSTALL_PREFIX}/lib/${ARCH}"
echo "    export GI_TYPELIB_PATH=${TYPELIB_DIR}"
echo "    ${GST_BIN} --version"
echo ""
echo "  To activate for RMS (when ready), add to rms-cam*.service [Service]:"
echo "    Environment=GST_PLUGIN_PATH=${INSTALL_PREFIX}/lib/${ARCH}/gstreamer-1.0"
echo "    Environment=LD_LIBRARY_PATH=${INSTALL_PREFIX}/lib/${ARCH}"
echo "    Environment=GI_TYPELIB_PATH=${TYPELIB_DIR}"
echo ""
echo "  Then reload: sudo systemctl daemon-reload && sudo systemctl restart rms-cam1 rms-cam2"
echo ""
