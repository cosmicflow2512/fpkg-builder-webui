# fPKG Builder WebUI

This WebUI wraps `build-fpkg.ps1` with a small browser interface and a local HTTP API.

## Run locally

```bash
python3 webui/server.py
```

Open `http://localhost:8080`.

## Docker image

```bash
docker build -f Dockerfile.webui -t fpkg-builder-webui:local .
docker run --rm -p 8080:8080 \
  -v /mnt/user/games:/mnt/user/games \
  -v /mnt/user/backports:/mnt/user/backports \
  -v /mnt/user/fpkg-output:/mnt/user/fpkg-output \
  -v /mnt/cache/appdata/fpkg-builder:/mnt/cache/appdata/fpkg-builder \
  fpkg-builder-webui:local
```

## Unraid

1. Upload this repository to `https://github.com/cosmicflow2512/fpkg-builder-webui`.
2. Wait for the `Publish container` GitHub Actions workflow to finish.
3. If the package is private, open the package settings on GitHub and set its visibility to public, or run `docker login ghcr.io` on Unraid.
4. Copy `deploy/unraid/fpkg-builder-webui.xml` to `/boot/config/plugins/dockerMan/templates-user/`.
5. In Unraid, open Docker, add a container, and choose the `fpkg-builder-webui` user template.
6. Adjust the host paths for games, backports, output, and appdata.

## Compatibility note

The upstream builder currently declares Windows, PowerShell 5.1, and Python 3 in the project README. The repository also ships Windows `.exe` publisher tools. This WebUI exposes the build workflow and logs, but it does not make those publisher tools Linux-native. On Unraid, use a runtime that can execute the configured PowerShell and publisher toolchain.
