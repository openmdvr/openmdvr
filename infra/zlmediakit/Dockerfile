# ZLMediaKit with AAC -> Opus audio transcoding for WebRTC.
#
# Why this exists: live video is played over WebRTC, and WebRTC does not
# accept AAC, the audio codec JC261/JC400 dashcams push over RTMP. The
# official zlmediakit/zlmediakit image does NOT link FFmpeg (its Dockerfile
# passes -DENABLE_FFMPEG=true but does not install the development libraries,
# so CMake silently disables it), and the transcoding feature
# (protocol.audio_transcode) lives in the upstream feature/transcode2 branch,
# not in master.
#
# What is built, pinned so it is reproducible and auditable:
#   - ZLMediaKit master @ 296dcebe (the same commit as the official image,
#     sha256:fbaed875...), submodules at their pinned commits.
#   - audio-transcode.patch: the upstream feature/transcode2 branch
#     (@03526f14, written by the ZLMediaKit authors) merged on top of that
#     commit, with 2 trivial conflicts resolved by hand (a
#     `private:`->`protected:` in RtspMediaSourceMuxer.h and two config.ini
#     keys). Audited: no new network/exec calls.
#   - Ubuntu 24.04 FFmpeg (libavcodec etc.) to decode AAC and encode Opus.
#
# Transcoding only runs while a WebRTC viewer is connected
# (RtcMediaSourceMuxer::inputFrame checks readerCount()); it costs no CPU
# when nobody is watching.
#
# To upgrade ZLMediaKit: regenerate the patch by merging feature/transcode2
# onto the new commit, update ZLM_COMMIT, rebuild and re-verify audio
# playback over WebRTC.

FROM ubuntu:24.04 AS build
ARG MODEL=Release
ARG ZLM_COMMIT=296dcebe8f3e81121e2c835cc9503ac5c25e2a1a
ARG LIBSRTP_VERSION=2.3.0

RUN apt-get update && \
    DEBIAN_FRONTEND="noninteractive" apt-get install -y --no-install-recommends \
        build-essential cmake git curl wget ca-certificates tzdata \
        libssl-dev gcc g++ \
        libavcodec-dev libavutil-dev libswscale-dev libswresample-dev libavfilter-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/media
# Submodules: ZLMediaKit's .gitmodules points to gitee.com mirrors (slow and
# unreliable outside China), so they are redirected to the official GitHub
# repositories. The COMMITS are still the ones pinned by ZLMediaKit at
# ${ZLM_COMMIT} (git verifies the SHA), so the code is identical. Only the 3
# the build needs: pybind11 is for the (disabled) Python plugin and
# www/webassist is an unused web UI.
RUN git clone https://github.com/ZLMediaKit/ZLMediaKit.git ZLMediaKit && \
    cd ZLMediaKit && \
    git checkout --detach "${ZLM_COMMIT}" && \
    git config submodule.ZLToolKit.url https://github.com/ZLMediaKit/ZLToolKit.git && \
    git config submodule.3rdpart/media-server.url https://github.com/ireader/media-server.git && \
    git config submodule.3rdpart/jsoncpp.url https://github.com/open-source-parsers/jsoncpp.git && \
    git submodule update --init 3rdpart/ZLToolKit 3rdpart/media-server 3rdpart/jsoncpp

COPY audio-transcode.patch /tmp/audio-transcode.patch
WORKDIR /opt/media/ZLMediaKit
RUN git apply --whitespace=nowarn /tmp/audio-transcode.patch

# libsrtp: same version as the official image (libsrtp2 2.3.0).
WORKDIR /opt/media/ZLMediaKit/3rdpart
RUN wget -q "https://github.com/cisco/libsrtp/archive/v${LIBSRTP_VERSION}.tar.gz" -O libsrtp.tar.gz && \
    tar xf libsrtp.tar.gz && mv "libsrtp-${LIBSRTP_VERSION}" libsrtp && \
    cd libsrtp && CFLAGS="-fcommon" ./configure --enable-openssl && make -j "$(nproc)" && make install

WORKDIR /opt/media/ZLMediaKit/build
RUN cmake -DCMAKE_BUILD_TYPE=${MODEL} -DENABLE_WEBRTC=true \
          -DENABLE_FFMPEG=true -DENABLE_TESTS=false -DENABLE_API=false .. && \
    make -j "$(nproc)" && \
    # Fail the build if FFmpeg ended up disabled; otherwise audio would
    # silently stay broken until someone tested it in the field.
    ldd "/opt/media/ZLMediaKit/release/linux/${MODEL}/MediaServer" | grep -q libavcodec

FROM ubuntu:24.04
ARG MODEL=Release

# ffmpeg: provides the libav* libraries MediaServer links against and the
# binary used by getSnap (preview snapshots), as in the official image.
RUN apt-get update && \
    DEBIAN_FRONTEND="noninteractive" apt-get install -y --no-install-recommends \
        ca-certificates tzdata curl libssl3 ffmpeg && \
    rm -rf /var/lib/apt/lists/* && \
    mkdir -p /opt/media/bin/www /opt/media/conf

WORKDIR /opt/media/bin/
COPY --from=build /opt/media/ZLMediaKit/release/linux/${MODEL}/MediaServer /opt/media/ZLMediaKit/default.pem /opt/media/bin/
COPY --from=build /opt/media/ZLMediaKit/release/linux/${MODEL}/config.ini /opt/media/conf/
COPY --from=build /opt/media/ZLMediaKit/www/ /opt/media/bin/www/
ENV PATH=/opt/media/bin:$PATH
CMD ["./MediaServer", "-s", "default.pem", "-c", "../conf/config.ini", "-l", "0"]
