from django.urls import path
from django.views.generic import RedirectView

from .views import (
                    table_view,
                    SessionsListView,
                    GridsListView,
                    GridsExportView
                    )

urlpatterns = [
    path("", table_view, name='sessionHistory'),
    path("sessions/", SessionsListView.as_view(), name="sessions"),
    path("grids/", GridsListView.as_view(), name="grids"),
    path("export/", GridsExportView.as_view(), name="gridsExport"),
]