"""Put the disposition helper's meaning-matching model where GREMLIN reads it, and check it.

The model is ``sentence-transformers/all-MiniLM-L6-v2`` (Apache-2.0), at the
revision ``services.disposition_helper_embedding`` pins, as ONNX: two files,
about 90 MB. Each is checked against its SHA-256 before it is put in place, so
a broken or different download is never used.

GREMLIN reads it from ``GREMLIN_EMBEDDING_MODEL_DIR`` or, by default, from
``embedding-model/all-MiniLM-L6-v2`` beside GREMLIN.db. It also needs the
``numpy``, ``onnxruntime`` and ``tokenizers`` packages (in requirements.txt).

Usage::

    python tools/fetch_embedding_model.py              # download from Hugging Face to where GREMLIN reads it
    python tools/fetch_embedding_model.py --dest D:\\models\\all-MiniLM-L6-v2
    python tools/fetch_embedding_model.py --from-dir E:\\all-MiniLM-L6-v2
                                                       # a server with no internet: copy the folder from a
                                                       # machine that ran this, and it is checked and put in place
    python tools/fetch_embedding_model.py --check      # will GREMLIN find it, load it, and does it read meaning?

GREMLIN picks the model up on its next start (or the next sync); the
Disposition page's helper bar says whether meaning matching is on.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import disposition_helper_embedding as embedding  # noqa: E402
from services.life_data_service import DEFAULT_DB_PATH  # noqa: E402
from services.sync_service import APP_ENV_KEYS, load_dotenv_files  # noqa: E402

DOWNLOAD_URL = "https://huggingface.co/{repo}/resolve/{revision}/{path}"


def _default_db_path() -> Path:
    override = os.environ.get("GREMLIN_DB_PATH")
    return Path(override) if override else DEFAULT_DB_PATH


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _place(source: Path, target: Path, expected: str) -> None:
    """Copy ``source`` to ``target`` only if it is the expected file."""

    actual = _sha256(source)
    if actual != expected:
        raise SystemExit(
            f"{source} is not the expected file (SHA-256 {actual}, expected {expected}). Nothing was changed."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != target.resolve():
        partial = target.with_name(target.name + ".part")
        shutil.copyfile(source, partial)
        os.replace(partial, target)


def download(dest: Path) -> None:
    import requests

    for name, expected in embedding.MODEL_FILES.items():
        target = dest / name
        if target.is_file() and _sha256(target) == expected:
            print(f"  {name}: already in place")
            continue
        url = DOWNLOAD_URL.format(repo=embedding.MODEL_REPO, revision=embedding.MODEL_REVISION, path=name)
        partial = target.with_name(target.name + ".part")
        partial.parent.mkdir(parents=True, exist_ok=True)
        print(f"  {name}: downloading {url}")
        try:
            with requests.get(url, stream=True, timeout=60) as response:
                response.raise_for_status()
                with open(partial, "wb") as handle:
                    for block in response.iter_content(chunk_size=1 << 20):
                        handle.write(block)
        except requests.RequestException as exc:
            partial.unlink(missing_ok=True)
            raise SystemExit(
                f"Could not download {name} from huggingface.co ({exc.__class__.__name__}). If this machine cannot "
                "reach it, run this tool on one that can, copy the model folder over, and install it with --from-dir."
            ) from None
        actual = _sha256(partial)
        if actual != expected:
            partial.unlink(missing_ok=True)
            raise SystemExit(f"{name} downloaded with SHA-256 {actual}, expected {expected}. Nothing was changed.")
        os.replace(partial, target)
        print(f"  {name}: checked and in place")


def copy_from(source_dir: Path, dest: Path) -> None:
    for name, expected in embedding.MODEL_FILES.items():
        source = source_dir / name
        if not source.is_file():
            raise SystemExit(f"{source} is missing; copy the whole folder, onnx/ included.")
        _place(source, dest / name, expected)
        print(f"  {name}: checked and in place")


def check(db_path: Path) -> int:
    encoder, status = embedding.availability(db_path)
    if encoder is None:
        print(f"Meaning matching is off: {status['reason']}")
        return 1
    near = encoder.encode(["Burner won't light", "No flame on startup", "Conveyor chain is noisy"])
    alike, unlike = float(near[0] @ near[1]), float(near[0] @ near[2])
    print(f"Meaning matching is on: {embedding.MODEL_NAME} from {status['model_dir']}.")
    print(f"  'Burner won't light' ~ 'No flame on startup': {alike:.2f}; ~ 'Conveyor chain is noisy': {unlike:.2f}")
    if alike <= unlike:
        print("  The model loaded but does not read meaning as expected; fetch it again.")
        return 1
    return 0


def main() -> int:
    load_dotenv_files(only_keys=APP_ENV_KEYS)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=None, help="GREMLIN.db (default: GREMLIN_DB_PATH or the default path)")
    parser.add_argument("--dest", type=Path, default=None, help="the model folder (default: where GREMLIN reads it)")
    parser.add_argument("--from-dir", type=Path, default=None, help="install from a copied folder instead of downloading")
    parser.add_argument("--check", action="store_true", help="only check that GREMLIN will find and load the model")
    args = parser.parse_args()
    db_path = args.db or _default_db_path()
    if args.check:
        return check(db_path)
    missing = embedding.missing_packages()
    dest = args.dest or embedding.settings(db_path)["model_dir"]
    print(f"Model folder: {dest}")
    if args.from_dir:
        copy_from(args.from_dir, dest)
    else:
        download(dest)
    if args.dest and Path(args.dest).resolve() != embedding.settings(db_path)["model_dir"].resolve():
        print(f"Set GREMLIN_EMBEDDING_MODEL_DIR={dest} (environment or .env) so GREMLIN reads it from there.")
    if missing:
        print(f"Install {', '.join(missing)} too: python -m pip install -r requirements.txt")
        return 0
    return check(db_path)


if __name__ == "__main__":
    raise SystemExit(main())
