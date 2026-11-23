from django.conf import settings
from django.http import HttpResponseForbidden

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
WRITE_ALLOWED = ("/sessions_history/export/", "/logout/")
BLOCKED_PREFIXES = ("/admin/", "/api-auth/", "/annotator/", "/selector_viewer/",
                    "/sim_siam/", "/smartscope/longactions/", "/smartscope/tags/")


def is_archive():
    return getattr(settings, "SMARTSCOPE_ROLE", "acquisition") == "archive"


class ArchiveReadOnlyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.active = is_archive()

    def __call__(self, request):
        user = getattr(request, "user", None)
        if self.active and user is not None and user.is_authenticated:
            path = request.path
            if user.is_staff:
                if request.method not in SAFE_METHODS and not (
                        path.startswith("/admin/") or path.startswith(WRITE_ALLOWED)):
                    return HttpResponseForbidden("Archive data is read-only; it only changes via import.")
            else:
                if path.startswith(BLOCKED_PREFIXES):
                    return HttpResponseForbidden("Not available in the archive.")
                if request.method not in SAFE_METHODS and not path.startswith(WRITE_ALLOWED):
                    return HttpResponseForbidden("Archive data is read-only.")
        return self.get_response(request)