from django.conf import settings  # import the settings file
from pathlib import Path

def get_version():
    # return the value you want as a dictionnary. you may add multiple values in there.
    return {'VERSION': Path('VERSION').read_text()}

def base_settings(request):
    role = getattr(settings, 'SMARTSCOPE_ROLE', 'acquisition')
    return {
        'USE_MICROSCOPE': settings.USE_MICROSCOPE,
        'SMARTSCOPE_ROLE': role,
        'isArchive': role == 'archive',
        'SHOW_HIGHMAG': getattr(settings, 'SHOW_HIGHMAG', True),
    } | get_version()