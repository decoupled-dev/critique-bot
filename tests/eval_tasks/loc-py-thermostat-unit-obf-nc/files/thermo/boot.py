""

import logging

from thermo.display import doIt4
from thermo.settings_store import Mgr2, doIt5, doIt6

log = logging.getLogger("thermo")


def doIt7(path, a1):
    a6 = Mgr2(path)
    a7 = doIt6(a6)
    log.info("restored display unit %s", a7)
    a5 = doIt5(a6)
    return doIt4(a1, a7), a5
