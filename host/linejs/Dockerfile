FROM node:24-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS build
ADD --checksum=sha256:437bc5dfafa038f2c29b3e58898c30dc47a560fef6a90d44c9c5058c2ffdb5d0 https://codeload.github.com/JS-PACKAGE/LINE.js/tar.gz/9670589e4df3096a06c812dc4c1298140c736172 /tmp/line.tar.gz
WORKDIR /opt/linejs
COPY patch-local-host.mjs /tmp/patch-local-host.mjs
RUN tar -xzf /tmp/line.tar.gz --strip-components=1 -C /opt/linejs && node /tmp/patch-local-host.mjs && npm ci --no-audit --no-fund && npm run build && npm prune --omit=dev

FROM node:24-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20
COPY --from=build /opt/linejs /opt/linejs
COPY start.mjs /opt/start.mjs
RUN chown -R root:root /opt/linejs /opt/start.mjs && chmod -R u=rwX,go=rX /opt/linejs /opt/start.mjs
USER 1000:1000
WORKDIR /data
CMD ["node", "/opt/start.mjs"]
