"""
Optimize + import a SmartScope grid export into THIS (archive-role) instance.

For one grid export it:
  1. reads the export YAML from a source working directory and upgrades older
     export formats (pre-2025-06) to the current one,
  2. ANONYMIZES System A's identity (who / where / when / which machine),
  3. imports the depurated metadata into this instance's database,
  4. copies the PNGs (atlas, squares, holes and high-mag) into the optimized store,
     under the ANONYMIZED layout the viewer resolves,
  5. writes the grid's export.yaml next to those PNGs (the portable, downloadable copy).

Anonymization and import are both IDEMPOTENT and DETERMINISTIC: re-running for the
same source grid maps to the same anonymous ids and replaces that grid (DB rows and
store folder) instead of duplicating it.

The archive instance therefore holds depurated metadata plus PNGs only: never
movies, raw/MRC files, CTF images, nor System A's identity. The raw working
directory is read-only input, never a destination.

CLI (inside the container), via the SmartScope command runner::

    ./smartscope.sh run optimize_import <export_yaml> [group] [user] [strip_highmag] [anonymize] [source_root] [store_root]

Defaults: group='' / user='' (keep the ones in the export), strip_highmag=False,
anonymize=True, source_root=/mnt/source, store_root=/mnt/optimized.
"""
import datetime as dt
import hashlib
import logging
import re
import shutil
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_TIMESTAMP_KEYS = frozenset({
    "creation_time", "start_time", "last_update",
    "loading_time", "unloading_time", "completion_time",
})
_BLANK_KEYS = frozenset({"notes", "frames"})

_ANON_MICROSCOPE = {
    "location": "archive",
    "worker_hostname": "localhost",
    "executable": "smartscope.py",
    "serialem_IP": "xxx.xxx.xxx.xxx",
    "serialem_PORT": 48888,
    "windows_path": "anonymized",
    "scope_path": "anonymized",
}

_ANON_DETECTOR = {
    "name": "detector",
    "frames_directory": "anonymized",
    "frames_windows_directory": "anonymized",
}

_MONTH_RE = re.compile(r"^(\d{4})-?(\d{2})")


def _coarse_month(value):
    if value in (None, ""):
        return None
    try:
        return value.year, value.month
    except AttributeError:
        pass
    m = _MONTH_RE.match(str(value))
    return (int(m.group(1)), int(m.group(2))) if m else None


# Coarse timestamps are anchored mid-month (day 15, 12:00 UTC): a first-of-month
# 00:00 UTC anchor is rendered as the previous month in any timezone west of UTC
# (the server runs on America/New_York).
def _coarsen_ts(value):
    ym = _coarse_month(value)
    return f"{ym[0]:04d}-{ym[1]:02d}-15T12:00:00Z" if ym else value


def _coarse_dt(value):
    ym = _coarse_month(value)
    return dt.datetime(ym[0], ym[1], 15, 12, 0, tzinfo=dt.timezone.utc) if ym else None


def _scrub_tree(node):
    if isinstance(node, dict):
        for key, val in node.items():
            if key in _TIMESTAMP_KEYS:
                node[key] = _coarsen_ts(val)
            elif key in _BLANK_KEYS:
                node[key] = None
            else:
                _scrub_tree(val)
    elif isinstance(node, list):
        for item in node:
            _scrub_tree(item)


def _stable_pseudonym(*parts, length: int = 10) -> str:

    digest = hashlib.sha256("\x00".join(str(p) for p in parts).encode()).hexdigest()
    return f"s{digest[:length]}"


def _anonymize(data: dict, orig_group) -> None:

    session = data["session_id"]
    orig_date = str(session.get("date", ""))
    orig_session = str(session.get("session", ""))

    ym = _coarse_month(orig_date)
    session["date"] = f"{ym[0]:04d}-{ym[1]:02d}" if ym else orig_date
    session["session"] = _stable_pseudonym(orig_group, orig_date, orig_session)

    detector = session.get("detector_id") or {}
    microscope = detector.get("microscope_id") or {}
    microscope.update({k: v for k, v in _ANON_MICROSCOPE.items() if k in microscope})
    detector.update({k: v for k, v in _ANON_DETECTOR.items() if k in detector})

    _scrub_tree(data)


def _upgrade_legacy_export(data: dict) -> dict:
    """Bring an older SmartScope export up to the current format, in place.

    1. Exports written before 2025-06 nest children under 'targets'; the current
       ExportMetaSerializer reads 'targets_export'.
    2. The same exports repeat every high-mag of a BIS group under each hole of
       the group (fixed upstream in 637590b). A high-mag is named
       '<hole name>_<number>_hm', so keep it only under the hole it belongs to.

    Both steps are no-ops on current exports.
    """
    stats = {"renamed": 0, "dropped_duplicate_hm": 0}

    def rename(node):
        if "targets" in node and "targets_export" not in node:
            node["targets_export"] = node.pop("targets")
            stats["renamed"] += 1

    for atlas in data.get("atlas") or []:
        rename(atlas)
        for square in atlas.get("targets_export") or []:
            rename(square)
            for hole in square.get("targets_export") or []:
                rename(hole)
                highmags = hole.get("targets_export") or []
                own = [hm for hm in highmags
                       if str(hm.get("name", "")) == f"{hole.get('name')}_{hm.get('number')}_hm"]
                stats["dropped_duplicate_hm"] += len(highmags) - len(own)
                hole["targets_export"] = own
    return stats


def _find_existing_session(session_data: dict):
    from Smartscope.core.models import ScreeningSession

    return ScreeningSession.objects.filter(
        date=str(session_data["date"]),
        session=str(session_data["session"]).replace(" ", "_"),
        group__name=session_data.get("group"),
    ).first()


def _copy_slim_pngs(src_pngs: Path, dst_pngs: Path) -> tuple:
    if not src_pngs.is_dir():
        logger.warning(f"No pngs/ directory at {src_pngs}; no images copied.")
        return 0, 0

    dst_pngs.mkdir(parents=True, exist_ok=True)
    copied = 0
    for img in src_pngs.iterdir():
        if not img.is_file() or img.suffix.lower() not in (".png", ".webp"):
            continue
        shutil.copy2(img, dst_pngs / img.name)
        copied += 1
    return copied, 0


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y"):
        return True
    if text in ("0", "false", "no", "n", ""):
        return False
    return default


def optimize_import(export_yaml: str,
                    override_group: str = "",
                    override_user: str = "",
                    strip_highmag=False,
                    anonymize=True,
                    source_root: str = "/mnt/source",
                    store_root: str = "/mnt/optimized") -> None:

    strip_highmag = _as_bool(strip_highmag, False)
    anonymize = _as_bool(anonymize, True)

    from django.db import transaction
    from rest_framework_yaml.renderers import YAMLRenderer
    from Smartscope.core.models import AutoloaderGrid
    from Smartscope.server.api.serializers.export_serializers import ExportMetaSerializer

    export_yaml = Path(export_yaml)
    with open(export_yaml) as fh:
        # C loader when available: legacy exports can be >10 MB.
        data = yaml.load(fh, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))

    stats = _upgrade_legacy_export(data)
    if stats["renamed"] or stats["dropped_duplicate_hm"]:
        print(f"Legacy export upgraded: {stats['renamed']} 'targets' keys renamed, "
              f"{stats['dropped_duplicate_hm']} duplicated high-mags dropped.", flush=True)

    raw_session = data["session_id"]

    orig_group = raw_session.get("group")
    orig_wd = f"{raw_session['date']}_{str(raw_session['session']).replace(' ', '_')}"
    orig_gdir = f"{data['position']}_{data['name']}"

    if override_group:
        raw_session["group"] = override_group
    if override_user:
        raw_session["user"] = override_user

    if strip_highmag:
        _strip_highmag(data)

    if anonymize:
        _anonymize(data, orig_group)

    existing_session = _find_existing_session(data["session_id"])
    if existing_session is not None:
        data["session_id"]["creation_time"] = existing_session.creation_time

    serializer = ExportMetaSerializer(data=data)
    serializer.is_valid()
    if serializer.errors:
        print(f"Import validation errors: {serializer.errors}", flush=True)
        return

    replaced = 0
    with transaction.atomic():
        if existing_session is not None:
            replaced, _ = AutoloaderGrid.objects.filter(
                session_id=existing_session,
                position=data["position"],
                name=data["name"],
            ).delete()
        obj = serializer.save()

    if anonymize:
        ct = _coarse_dt(data["session_id"].get("creation_time"))
        if ct is not None and existing_session is None:
            obj.session_id.creation_time = ct
            obj.session_id.save(update_fields=["creation_time"])

        grid_updates = {}
        for k in ("start_time", "loading_time", "unloading_time"):
            v = _coarse_dt(data.get(k))
            if v is not None:
                grid_updates[k] = v
        lu = (_coarse_dt(data.get("last_update"))
              or grid_updates.get("start_time") or ct)
        if lu is not None:
            grid_updates["last_update"] = lu
        if grid_updates:
            AutoloaderGrid.objects.filter(pk=obj.pk).update(**grid_updates)

    group = obj.session_id.group.name if obj.session_id.group else None
    wd = obj.session_id.working_directory          # anonymized '{YYYY-MM}_{sHASH}'
    gdir = f"{obj.position}_{obj.name}"

    src_pngs = Path(source_root) / orig_wd / orig_gdir / "pngs"
    if not src_pngs.is_dir():
        src_pngs = export_yaml.parent / "pngs"

    store_root = Path(store_root)
    grid_dst = store_root / group / wd / gdir if group else store_root / wd / gdir
    dst_pngs = grid_dst / "pngs"

    if grid_dst.is_dir() and store_root in grid_dst.parents:
        shutil.rmtree(grid_dst)

    copied, _ = _copy_slim_pngs(src_pngs, dst_pngs)

    grid_dst.mkdir(parents=True, exist_ok=True)
    yaml_bytes = YAMLRenderer().render(data=ExportMetaSerializer(instance=obj).data)
    (grid_dst / "export.yaml").write_bytes(yaml_bytes)

    action = "Replaced" if replaced else "Imported"
    anon = "anonymized" if anonymize else "RAW (not anonymized)"
    print(f"{action} {orig_wd}/{orig_gdir} -> {wd}/{gdir} "
          f"(group={group}, {anon}): {copied} PNGs copied.",
          flush=True)
    print(f"  metadata -> this DB   |   images + export.yaml -> {grid_dst}", flush=True)
    if copied == 0:
        print(f"  WARNING: 0 images copied. Checked {src_pngs} — adjust source_root "
              f"if the pngs/ folder lives elsewhere.", flush=True)


def _strip_highmag(data: dict) -> None:
    for atlas in data.get("atlas", []) or []:
        for square in atlas.get("targets_export", []) or []:
            for hole in square.get("targets_export", []) or []:
                hole["targets_export"] = []