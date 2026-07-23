# Claude Code harness mount image. The CLI version is supplied by harness YAML build args.

ARG CLAUDE_CODE_VERSION

FROM node:20-slim AS build
ARG CLAUDE_CODE_VERSION
ARG MOUNT_PATH=/opt/claude-code
ENV MOUNT_PATH=${MOUNT_PATH}
RUN test -n "${CLAUDE_CODE_VERSION}" && \
    npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"
# Write the `claude` launcher shim. Claude Code 2.x ships its bin
# (`bin/claude.exe`) as a self-contained native ELF binary, not a JS file, so the
# shim execs it directly rather than running it via `node`.
RUN set -eux; \
    mkdir -p /out/bin /out/lib; \
    cp -r /usr/local/lib/node_modules /out/lib/node_modules; \
    node -e 'const fs = require("fs"); const path = require("path"); const pkgPath = "/usr/local/lib/node_modules/@anthropic-ai/claude-code/package.json"; const pkg = JSON.parse(fs.readFileSync(pkgPath, "utf8")); const rawBin = pkg.bin; let claudeEntry; if (typeof rawBin === "string") { claudeEntry = rawBin; } else { const bin = rawBin && typeof rawBin === "object" ? rawBin : {}; claudeEntry = bin.claude; } if (!claudeEntry) { throw new Error("Cannot find claude bin entry in @anthropic-ai/claude-code package.json"); } const mountPath = process.env.MOUNT_PATH || "/opt/claude-code"; const normalized = claudeEntry.replace(/^\.\//, ""); const target = path.posix.join(mountPath, "lib/node_modules/@anthropic-ai/claude-code", normalized); fs.writeFileSync("/out/bin/claude", `#!/usr/bin/env bash\nexec "${target}" "$@"\n`, { mode: 0o755 });'

FROM scratch
COPY --from=build /out/bin/ /bin/
COPY --from=build /out/lib/ /lib/
