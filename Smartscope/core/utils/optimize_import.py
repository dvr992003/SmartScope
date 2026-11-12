"""
Optimize + import a SmartScope grid export into THIS (archive-role) instance.

For one grid export it:
  1. reads the export YAML from a source working directory,
  2. imports the metadata into this instance's database (high-mag metadata, incl.
     the quality classifiers, is kept by default; pass strip_highmag=True to drop it),
  3. copies the slim PNGs (everything except the *_hm micrograph images) into the
     optimized store, under the layout the viewer resolves (AUTOSCREENDIR/<group>/...).

The archive instance therefore holds the metadata plus the lightweight PNGs, but
never the heavy high-mag micrograph images. The raw working directory is
read-only input, never a destination.

CLI (inside the container), via the SmartScope command runner::

    ./smartscope.sh run optimize_import <export_yaml> [group] [source_root] [store_root]

Defaults: group='' (keep the one in the export), source_root=/mnt/source,
store_root=/mnt/optimized (this instance's AUTOSCREENDIR).
"""
import logging
import shutil
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Images we never copy: high-mag micrographs. CTF/raw never live in pngs/, so
# excluding *_hm is enough to drop every high-mag-derived image.
HIGHMAG_SUFFIXES = ("_hm.png", "_hm.webp")


def _strip_highmag(data: dict) -> None:
    """Empty every hole's high-mag list, in place.

    The export nests atlas -> squares -> holes -> highmags under the key
    ``targets_export``. Setting each hole's ``targets_export`` to ``[]`` keeps the
    field present (the import's create() still iterates it) but imports zero
    high-mag targets and labels.
    """
    for atlas in data.get("atlas", []) or []:
        for square in atlas.get("targets_export", []) or []:
            for hole in square.get("targets_export", []) or []:
                hole["targets_export"] = []


def _copy_slim_pngs(src_pngs: Path, dst_pngs: Path) -> tuple[int, int]:
    """Copy every PNG/WEBP from ``src_pngs`` to ``dst_pngs`` except high-mag."""
    if not src_pngs.is_dir():
        logger.warning(f"No pngs/ directory at {src_pngs}; no images copied.")
        return 0, 0

    dst_pngs.mkdir(parents=True, exist_ok=True)
    copied = skipped = 0
    for img in src_pngs.iterdir():
        if not img.is_file() or img.suffix.lower() not in (".png", ".webp"):
            continue
        if img.name.endswith(HIGHMAG_SUFFIXES):
            skipped += 1
            continue
        shutil.copy2(img, dst_pngs / img.name)
        copied += 1
    return copied, skipped


def optimize_import(export_yaml: str,
                    override_group: str = "",
                    override_user: str = "",
                    strip_highmag: bool = False,
                    source_root: str = "/mnt/source",
                    store_root: str = "/mnt/optimized") -> None:
    """Import one grid export into this DB and copy its slim PNGs.

    High-mag METADATA (including the quality classifiers used to train models) is
    kept by default; high-mag IMAGES are never copied (see _copy_slim_pngs). Pass
    strip_highmag=True to also drop the high-mag metadata.
    """
    # Imported lazily so the module imports without Django configured.
    from Smartscope.server.api.serializers.export_serializers import ExportMetaSerializer

    export_yaml = Path(export_yaml)
    with open(export_yaml) as fh:
        data = yaml.safe_load(fh)

    if override_group:
        data["session_id"]["group"] = override_group
    if override_user:
        data["session_id"]["user"] = override_user

    # 1. Optionally drop high-mag metadata. Default: keep it — the quality
    #    classifiers live here. High-mag IMAGES are excluded in the copy below.
    if strip_highmag:
        _strip_highmag(data)

    # 2. Import the depurated metadata into THIS instance's database.
    serializer = ExportMetaSerializer(data=data)
    serializer.is_valid()
    if serializer.errors:
        print(f"Import validation errors: {serializer.errors}")
        return
    grid = serializer.save()

    # 3. Resolve the layout the viewer expects and copy the slim PNGs.
    group = grid.session_id.group.name if grid.session_id.group else None
    wd = grid.session_id.working_directory          # '{date}_{session}'
    gdir = f"{grid.position}_{grid.name}"

    src_pngs = Path(source_root) / wd / gdir / "pngs"
    if not src_pngs.is_dir():
        # Fallback: pngs sitting next to the export file (grid-level export).
        src_pngs = export_yaml.parent / "pngs"

    dst_pngs = Path(store_root)
    if group:
        dst_pngs = dst_pngs / group
    dst_pngs = dst_pngs / wd / gdir / "pngs"

    copied, skipped = _copy_slim_pngs(src_pngs, dst_pngs)
    print(f"Imported {wd}/{gdir} (group={group}): "
          f"{copied} PNGs copied, {skipped} high-mag skipped.")
    print(f"  metadata -> this DB   |   images -> {dst_pngs}")
    if copied == 0:
        print(f"  WARNING: 0 images copied. Checked {src_pngs} — adjust source_root "
              f"if the pngs/ folder lives elsewhere (do NOT re-run the import).")
