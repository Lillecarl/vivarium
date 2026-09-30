"""Write an OCI image archive with exactly the layers asked for.

    oci-image.py OUT.tar REF [DIR ...]

Each DIR argument is one layer: comma-separated paths, where a name
ending in "/" is a directory and anything else an empty file. No DIR at all
is an image with zero layers, as `FROM scratch` with nothing added gives.
"""

import hashlib
import io
import json
import sys
import tarfile


def blob(data: bytes) -> tuple[str, bytes]:
    return "sha256:" + hashlib.sha256(data).hexdigest(), data


def layer(paths: list[str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in paths:
            info = tarfile.TarInfo(path.rstrip("/"))
            info.uid = info.gid = 0
            info.mtime = 0
            if path.endswith("/"):
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            else:
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(b""))
    return buf.getvalue()


def main() -> None:
    out, ref, *entries = sys.argv[1:]
    layers = [blob(layer(entry.split(","))) for entry in entries]
    config = blob(
        json.dumps(
            {
                "architecture": "amd64",
                "os": "linux",
                "config": {},
                "rootfs": {"type": "layers", "diff_ids": [d for d, _ in layers]},
            }
        ).encode()
    )
    manifest = blob(
        json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "digest": config[0],
                    "size": len(config[1]),
                },
                "layers": [
                    {
                        "mediaType": "application/vnd.oci.image.layer.v1.tar",
                        "digest": d,
                        "size": len(b),
                    }
                    for d, b in layers
                ],
            }
        ).encode()
    )
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": manifest[0],
                "size": len(manifest[1]),
                "annotations": {"io.containerd.image.name": ref, "org.opencontainers.image.ref.name": ref},
            }
        ],
    }
    with tarfile.open(out, "w") as tar:

        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        add("oci-layout", json.dumps({"imageLayoutVersion": "1.0.0"}).encode())
        add("index.json", json.dumps(index).encode())
        for digest, data in [config, manifest, *layers]:
            add("blobs/sha256/" + digest.removeprefix("sha256:"), data)


if __name__ == "__main__":
    main()
