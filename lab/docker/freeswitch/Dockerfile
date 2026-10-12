# FreeSWITCH lab image, built from the public release tarball.
# The SignalWire apt repository requires an access token, so it is deliberately not used.
ARG DEBIAN_IMAGE=debian:bookworm-slim

FROM ${DEBIAN_IMAGE} AS build
ARG FS_VERSION=1.10.12
ARG FS_SHA256=8411564621c7504e7628693f0ee4642b1245dbac050bcc11bbb51c15a2692a55
# sofia-sip v1.13.17 and spandsp v3.1.1 (commit SHAs; Debian bookworm packages are too old)
ARG SOFIA_SIP_REF=6198851a610b7889c17e2d98fb84617bc1dd7aec
ARG SPANDSP_REF=8f1e1646bdec99eac5fd2cd92c35563f736b9b89

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates curl pkg-config autoconf automake libtool \
      libssl-dev zlib1g-dev libcurl4-openssl-dev libpcre3-dev libspeex-dev libspeexdsp-dev \
      libedit-dev libsqlite3-dev libldns-dev libtiff-dev libjpeg-dev uuid-dev libglib2.0-dev \
    && rm -rf /var/lib/apt/lists/*

ENV PKG_CONFIG_PATH=/usr/local/lib/pkgconfig

WORKDIR /src
RUN curl -fsSL -o sofia.tgz https://github.com/freeswitch/sofia-sip/archive/${SOFIA_SIP_REF}.tar.gz \
    && mkdir sofia && tar -xzf sofia.tgz -C sofia --strip-components=1 \
    && cd sofia && ./bootstrap.sh && ./configure --prefix=/usr/local && make -j"$(nproc)" && make install
RUN curl -fsSL -o spandsp.tgz https://github.com/freeswitch/spandsp/archive/${SPANDSP_REF}.tar.gz \
    && mkdir spandsp && tar -xzf spandsp.tgz -C spandsp --strip-components=1 \
    && cd spandsp && ./bootstrap.sh && ./configure --prefix=/usr/local && make -j"$(nproc)" && make install
RUN ldconfig

RUN curl -fsSL -o fs.tgz https://files.freeswitch.org/releases/freeswitch/freeswitch-${FS_VERSION}.-release.tar.gz \
    && echo "${FS_SHA256}  fs.tgz" | sha256sum -c - \
    && mkdir freeswitch && tar -xzf fs.tgz -C freeswitch --strip-components=1
WORKDIR /src/freeswitch
COPY modules.conf /src/freeswitch/modules.conf
RUN ./configure --prefix=/usr/local/freeswitch --disable-debug --disable-zrtp \
    && make -j"$(nproc)" && make install \
    && rm -rf /usr/local/freeswitch/etc /usr/local/freeswitch/htdocs /usr/local/freeswitch/fonts /usr/local/freeswitch/images

FROM ${DEBIAN_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates python3 libssl3 zlib1g libcurl4 libpcre3 libspeex1 libspeexdsp1 libedit2 \
      libsqlite3-0 libldns3 libtiff6 libjpeg62-turbo libuuid1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /usr/local/lib/libsofia-sip-ua*.so* /usr/local/lib/libspandsp*.so* /usr/local/lib/
COPY --from=build /usr/local/freeswitch /usr/local/freeswitch
COPY bin/ /usr/local/bin/
RUN ldconfig \
    && groupadd -r --gid=999 freeswitch && useradd -r -g freeswitch --uid=999 freeswitch \
    && ln -s /usr/local/freeswitch/bin/fs_cli /usr/local/bin/fs_cli \
    && ln -s /usr/local/freeswitch/bin/freeswitch /usr/local/bin/freeswitch \
    && mkdir -p /usr/local/freeswitch/conf /usr/local/freeswitch/log /usr/local/freeswitch/db /usr/local/freeswitch/run \
    && chown -R freeswitch:freeswitch /usr/local/freeswitch

USER freeswitch
VOLUME ["/usr/local/freeswitch/log", "/usr/local/freeswitch/db"]
# -nf: stay in foreground (console log goes to stdout => `docker compose logs`),
# -np: no realtime priority (needs privileges); -nonat/-nonatmap: no UPnP/NAT-PMP probing. Do not add -nc: it silences container logs.
# The explicit dirs matter: the build's default conf dir is /usr/local/freeswitch/etc/freeswitch.
ENTRYPOINT ["/usr/local/freeswitch/bin/freeswitch"]
CMD ["-nf", "-np", "-nonat", "-nonatmap", \
     "-conf", "/usr/local/freeswitch/conf", \
     "-log", "/usr/local/freeswitch/log", \
     "-db", "/usr/local/freeswitch/db", \
     "-run", "/usr/local/freeswitch/run"]
