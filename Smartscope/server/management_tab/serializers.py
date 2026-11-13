from rest_framework import serializers
from Smartscope.core.models.screening_session import ScreeningSession
from Smartscope.core.models.grid import AutoloaderGrid

class SessionSerializer(serializers.ModelSerializer):
    group = serializers.CharField(source="group.name", read_only=True)
    group_id = serializers.CharField(source="group.id", read_only=True)
    microscope = serializers.CharField(source="microscope_id.name", read_only=True)
    user = serializers.CharField(source="user.username", read_only=True)
    last_update = serializers.DateTimeField(read_only=True)
    grid_id = serializers.CharField(read_only=True)
    grid_count = serializers.IntegerField(read_only=True)
    grid_good = serializers.IntegerField(read_only=True)
    grid_bad = serializers.IntegerField(read_only=True)
    session_type = serializers.CharField()
    session_label = serializers.SerializerMethodField()
    date = serializers.CharField(read_only=True)
    session = serializers.CharField(read_only=True)
    detector = serializers.SerializerMethodField()

    def get_session_label(self, obj):
        return f"{obj.date}_{obj.session}"

    def get_detector(self, obj):
        det = getattr(obj, 'detector_id', None)
        return det.detector_model if det else None

    class Meta:
        model = ScreeningSession
        fields = [
            "session_label", "session_id", "group", "group_id",
            "microscope", "detector", "user", "date", "session",
            "creation_time", "last_update", "session_type",
            "grid_id", "grid_count", "grid_good", "grid_bad",
        ]


class GridSerializer(serializers.ModelSerializer):
    session_label = serializers.SerializerMethodField()
    session_id = serializers.CharField(source="session_id.session_id", read_only=True)
    group_id = serializers.CharField(source="session_id.group.id", read_only=True)
    date = serializers.CharField(source="session_id.date", read_only=True)
    microscope = serializers.CharField(source="session_id.microscope_id.name", read_only=True)
    detector = serializers.SerializerMethodField()
    holeType = serializers.CharField(source="holeType_id", read_only=True)
    session_type = serializers.CharField(source="collection_mode", read_only=True)
    grid_name = serializers.CharField(source="name", read_only=True)
    squares_acquired = serializers.IntegerField(read_only=True)
    holes_acquired = serializers.IntegerField(read_only=True)

    def get_session_label(self, obj):
        s = obj.session_id
        return f"{s.date}_{s.session}"

    def get_detector(self, obj):
        det = getattr(obj.session_id, "detector_id", None)
        return det.detector_model if det else None

    class Meta:
        model = AutoloaderGrid
        fields = [
            "grid_id", "grid_name", "session_label", "session_id", "group_id",
            "date", "microscope", "detector", "holeType", "session_type",
            "status", "squares_acquired", "holes_acquired",
        ]