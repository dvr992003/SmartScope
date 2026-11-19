import json
import logging
from zoneinfo import ZoneInfo
from datetime import datetime

from django.shortcuts import render
from django.db.models import Q, Prefetch, Avg, Max, Min, Count, Case, When, Value, CharField

from rest_framework.views import APIView
from rest_framework.response import Response

from .serializers import SessionSerializer
from Smartscope.core.models.screening_session import ScreeningSession
from Smartscope.core.models.grid import AutoloaderGrid
from Smartscope.core.settings import server_docker
# from core.models import Product
from django.conf import settings
from rest_framework.exceptions import PermissionDenied
from .serializers import SessionSerializer, GridSerializer
from django.db.models import OuterRef, Subquery, IntegerField
from django.db.models.functions import Coalesce
from Smartscope.core.models.square import SquareModel
from Smartscope.core.models.hole import HoleModel
import tempfile, zipfile
from pathlib import Path
from django.http import FileResponse



logger = logging.getLogger(__name__)


def _is_archive():
    return getattr(settings, "SMARTSCOPE_ROLE", "acquisition") == "archive"

GRID_FILTER_FIELD_MAP = {
    "date": "session_id__date",
    "microscope": "session_id__microscope_id__name",
    "detector": "session_id__detector_id__detector_model",
    "holeType": "holeType__name",
    "session_type": "collection_mode",
    "grid_name": "name",
    "squares_acquired": "squares_acquired",
    "holes_acquired": "holes_acquired",
    "grid_quality": "quality",
}
NUMERIC_GRID_FIELDS = {"squares_acquired", "holes_acquired"}
LOOKUPS = {"contains": "icontains", "equals": "exact",
           "greaterThan": "gt", "lessThan": "lt"}

FILTER_FIELD_MAP = {
    "group": "group__name",
    "microscope": "microscope_id__name",
    "user": "user__username",
    "detector": "detector_id__detector_model",
}

def table_view(request):
    if request.headers.get('HX-Request'):
        return render(request, "management_table.html")
    return render(request, 'autoscreenViewer/auto_screen_viewer.html', {
        'initial_partial': 'management_table.html',
    })

def utc_time_conversion(date: str,):
    try:
        naive_dt = datetime.strptime(date, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            # Fall back to date only, treat as local midnight
            naive_dt = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return None
    local_dt = naive_dt.replace(tzinfo=ZoneInfo(server_docker.TIME_ZONE))
    return local_dt.astimezone(ZoneInfo("UTC"))
        
def _completed_count(model):
    return Coalesce(Subquery(
        model.objects.filter(grid_id=OuterRef('pk'), status='completed')
             .values('grid_id').annotate(c=Count('*')).values('c')[:1],
        output_field=IntegerField(),
    ), 0)

class SessionsListView(APIView):
    # permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = (ScreeningSession.objects
                    .select_related(
                        "group", "microscope_id", "user"
                    ).annotate(
                        grid_count=Count("autoloadergrid__grid_id"),
                        grid_id=Min("autoloadergrid__grid_id"),
                        last_update=Max("autoloadergrid__last_update"),
                        avg_holes_per_square=Avg("autoloadergrid__params_id_id__holes_per_square"),
                        grid_good=Count("autoloadergrid__quality", filter=Q(autoloadergrid__quality='good')),
                        grid_bad=Count("autoloadergrid__quality", filter=Q(autoloadergrid__quality='bad'))
                    ).annotate(
                        session_type=Case(
                            When(avg_holes_per_square=0, then=Value('collection')),
                            When(avg_holes_per_square__isnull=True, then=Value('unknown')),
                            default=Value('screening'),
                            output_field=CharField()
                        )
                    ).order_by("-creation_time")
                )

        filter_params = self.request.GET.get("filter", None)
        if filter_params:
            filters = json.loads(filter_params)
            q_objects = []

            for key, filter_info in filters.items():
                filter_type = filter_info.get("type")
                filter_value = filter_info.get("filter")
                print(f"key - {key}, filter - {filter_type}, value - {filter_value}")

                if filter_value is None:
                    continue

                if  key == "session_label": 
                    q_objects.append(Q(**{f"session__icontains": filter_value}) | Q(**{f"date__icontains": filter_value}))
                    continue
                if key in ["grid_count", "grid_bad", "grid_good"]:
                    filter_value = int(filter_value)
                if key in ["creation_time", "last_update"] and filter_value:
                    filter_value = utc_time_conversion(filter_value)

                db_field = FILTER_FIELD_MAP.get(key, key)
                if filter_type == "contains":
                    q_objects.append(Q(**{f"{db_field}__icontains": filter_value}))
                elif filter_type == "equals":
                    q_objects.append(Q(**{f"{db_field}__exact": filter_value}))
                elif filter_type == "greaterThan":
                    q_objects.append(Q(**{f"{db_field}__gt": filter_value}))
                elif filter_type == "lessThan":
                    q_objects.append(Q(**{f"{db_field}__lt": filter_value}))

            queryset = queryset.filter(*q_objects)

        sort_params = self.request.GET.get("sort", None)
        if sort_params:
            sort_fields = []
            for s in json.loads(sort_params):
                key = FILTER_FIELD_MAP.get(s["colId"], s["colId"])
                if key == "session_label":
                    sort_fields.append("date" if s["sort"] == "asc" else f"-date")
                    key = "session"
                sort_fields.append(key if s["sort"] == "asc" else f"-{key}")
            if sort_fields:
                queryset = queryset.order_by(*sort_fields)

        return queryset

    def get(self, request, *args, **kwargs):
        if _is_archive():
            raise PermissionDenied("Session list is not available in archive role.")
        start_row = int(request.GET.get("startRow", 0))
        end_row = int(request.GET.get("endRow", 100))
        user = request.user
        logger.debug(f"{request.user}, {user.groups.values_list('id', flat=True)}")
        # print(user, user.groups)

        queryset = self.get_queryset()
        if user.is_staff:
            total_rows = queryset.count()
            page = queryset[start_row:end_row]
        else:
            subset = queryset.filter(group__in=user.groups.values_list("name", flat=True))
            total_rows = subset.count()
            page = subset[start_row:end_row]

        serializer = SessionSerializer(page, many=True)
        return Response({"rows": serializer.data, "totalRows": total_rows})

class GridsListView(APIView):

    def get_queryset(self):
        qs = (AutoloaderGrid.objects
              .select_related("session_id", "session_id__group",
                              "session_id__microscope_id", "session_id__detector_id",
                              "holeType")
              .annotate(
            squares_acquired=_completed_count(SquareModel),
            holes_acquired=_completed_count(HoleModel),
        )
              .order_by("-session_id__creation_time", "position"))

        filter_params = self.request.GET.get("filter")
        if filter_params:
            q_objects = []
            for key, info in json.loads(filter_params).items():
                ftype, fval = info.get("type"), info.get("filter")
                if fval is None or fval == "":
                    continue
                if key == "session_label":
                    q_objects.append(Q(session_id__session__icontains=fval) |
                                     Q(session_id__date__icontains=fval))
                    continue
                if key in NUMERIC_GRID_FIELDS:
                    try:
                        fval = int(fval)
                    except (TypeError, ValueError):
                        continue
                lookup = LOOKUPS.get(ftype)
                if lookup:
                    db_field = GRID_FILTER_FIELD_MAP.get(key, key)
                    q_objects.append(Q(**{f"{db_field}__{lookup}": fval}))
            qs = qs.filter(*q_objects)

        sort_params = self.request.GET.get("sort")
        if sort_params:
            sort_fields = []
            for s in json.loads(sort_params):
                key = GRID_FILTER_FIELD_MAP.get(s["colId"], s["colId"])
                if s["colId"] == "session_label":
                    key = "session_id__session"
                sort_fields.append(key if s["sort"] == "asc" else f"-{key}")
            if sort_fields:
                qs = qs.order_by(*sort_fields)
        return qs

    def get(self, request, *args, **kwargs):
        if not _is_archive():
            raise PermissionDenied("Grid list is only available in archive role.")
        start = int(request.GET.get("startRow", 0))
        end = int(request.GET.get("endRow", 100))
        qs = self.get_queryset()
        if not request.user.is_staff:
            qs = qs.filter(session_id__group__in=request.user.groups.values_list("name", flat=True))
        total = qs.count()
        page = qs[start:end]
        return Response({"rows": GridSerializer(page, many=True).data, "totalRows": total})

class GridsExportView(APIView):

    def post(self, request, *args, **kwargs):
        if not _is_archive():
            raise PermissionDenied("Export is only available in archive role.")

        grid_ids = request.data.get("grid_ids") or []
        if not grid_ids:
            return Response({"detail": "No grids selected."}, status=400)

        qs = (AutoloaderGrid.objects
              .select_related("session_id")
              .filter(grid_id__in=grid_ids))
        if not request.user.is_staff:
            qs = qs.filter(session_id__group__in=request.user.groups.values_list("name", flat=True))

        # SpooledTemporaryFile: RAM hasta 64MB, luego disco; se borra al cerrarse.
        buf = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
        written = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            for grid in qs:
                gdir = Path(grid.directory)
                if not gdir.is_dir():
                    continue
                s = grid.session_id
                base = f"{s.date}_{s.session}/{grid.position}_{grid.name}"
                yml = gdir / "export.yaml"
                if yml.is_file():
                    zf.write(yml, f"{base}/export.yaml")
                    written += 1
                pngs = gdir / "pngs"
                if pngs.is_dir():
                    for f in sorted(pngs.iterdir()):
                        if f.is_file():
                            zf.write(f, f"{base}/pngs/{f.name}")
                            written += 1

        if written == 0:
            return Response({"detail": "Nothing to export."}, status=404)

        buf.seek(0)
        filename = f"smartscope_export_{qs.count()}grids.zip"
        return FileResponse(buf, as_attachment=True, filename=filename,
                            content_type="application/zip")