from . import cloud; assert cloud
from . import db; assert db

__version__ = '1.3'

# null progress, can be overridden by importers
def _progress(p):
    pass

progress = _progress

