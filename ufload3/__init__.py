from . import cloud; assert cloud
from . import db; assert db

__version__ = '1.6'

# null progress, can be overridden by importers
def _progress(p):
    pass

progress = _progress

