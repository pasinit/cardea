"""Development-only daemon launcher: the real `serve()` with the UNENFORCED dev identity."""

import logging
import sys
from pathlib import Path

from cardea.service.database import private_file
from cardea.service.models import ServiceConfig
from cardea.service.server import serve
from devauth import DevAuthentication

config = ServiceConfig.model_validate_json(private_file(Path(sys.argv[1])))
logging.getLogger("cardea.try").warning(
    "UNENFORCED development identity: never use this launcher for real work"
)
serve(config, identity=DevAuthentication)
