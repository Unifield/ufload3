from . import cloud; assert cloud
from . import db; assert db
from . import httpfile; assert httpfile

__version__ = '1.000'

# null progress, can be overridden by importers
def _progress(p):
    pass

progress = _progress

