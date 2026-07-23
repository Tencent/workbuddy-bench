# CodeBuddy Code harness mount image. The CLI version is supplied by harness YAML build args.

ARG CBC_VERSION

FROM node:20-slim AS build
ARG CBC_VERSION
ARG MOUNT_PATH=/opt/codebuddy-code
ENV MOUNT_PATH=${MOUNT_PATH}
RUN test -n "${CBC_VERSION}" && \
    npm install -g "@tencent-ai/codebuddy-code@${CBC_VERSION}"
RUN set -eux; \
    mkdir -p /out/bin /out/lib; \
    cp -r /usr/local/lib/node_modules /out/lib/node_modules; \
    cp /usr/local/bin/node /out/bin/node; \
    node -e 'const fs = require("fs"); const path = require("path"); const pkgPath = "/usr/local/lib/node_modules/@tencent-ai/codebuddy-code/package.json"; const pkg = JSON.parse(fs.readFileSync(pkgPath, "utf8")); const rawBin = pkg.bin; let cbcEntry; let codebuddyEntry; if (typeof rawBin === "string") { cbcEntry = rawBin; codebuddyEntry = rawBin; } else { const bin = rawBin && typeof rawBin === "object" ? rawBin : {}; cbcEntry = bin.cbc || bin.codebuddy; codebuddyEntry = bin.codebuddy || bin.cbc; } if (!cbcEntry || !codebuddyEntry) { throw new Error("Cannot find cbc/codebuddy bin entries in @tencent-ai/codebuddy-code package.json"); } const mountPath = process.env.MOUNT_PATH || "/opt/codebuddy-code"; function writeLauncher(name, entry) { const normalized = entry.replace(/^\.\//, ""); const target = path.posix.join(mountPath, "lib/node_modules/@tencent-ai/codebuddy-code", normalized); const nodeBin = path.posix.join(mountPath, "bin/node"); fs.writeFileSync(`/out/bin/${name}`, `#!/usr/bin/env bash\nexec "${nodeBin}" "${target}" "$@"\n`, { mode: 0o755 }); } writeLauncher("cbc", cbcEntry); writeLauncher("codebuddy", codebuddyEntry);'

FROM scratch
COPY --from=build /out/bin/ /bin/
COPY --from=build /out/lib/ /lib/
