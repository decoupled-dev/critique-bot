"""Pass when vehicle_logger.te gains the one allow rule the enforced denial needs, and nothing broader.

Accepted: allow vendor_vehicle_logger vendor_vehicle_bus:unix_stream_socket connectto;
(or the same with braces), or unix_socket_connect(vendor_vehicle_logger, vehicle_bus, vendor_vehicle_bus).
Rejected: the original rules changed or removed, permissive statements, wildcards or
extra permissions/classes/types, or edits to other policy files.
"""

import re
import sys

ORIGINAL = [
    "type vendor_vehicle_logger, domain;",
    "type vendor_vehicle_logger_exec, exec_type, vendor_file_type, file_type;",
    "init_daemon_domain(vendor_vehicle_logger)",
    "allow vendor_vehicle_logger vendor_vehicle_log_file:dir create_dir_perms;",
    "allow vendor_vehicle_logger vendor_vehicle_log_file:file create_file_perms;",
    "allow vendor_vehicle_logger vendor_vehicle_bus_socket:sock_file write;",
    "get_prop(vendor_vehicle_logger, vendor_vehicle_logger_prop)",
]
BUS_TE = """# Vehicle bus daemon: owns /dev/socket/vehicle_bus.
type vendor_vehicle_bus, domain;
type vendor_vehicle_bus_exec, exec_type, vendor_file_type, file_type;
type vendor_vehicle_bus_socket, file_type;

init_daemon_domain(vendor_vehicle_bus)
"""


def norm(line):
    return re.sub(r"\s+", " ", line.strip())


problems = []
text = open("sepolicy/vendor/vehicle_logger.te", encoding="utf-8").read()
lines = [norm(l.split("#", 1)[0]) for l in text.splitlines()]
lines = [l for l in lines if l]
for want in ORIGINAL:
    if want not in lines:
        problems.append(f"original rule missing or changed: {want}")
extra = [l for l in lines if l not in ORIGINAL]
rule = re.compile(r"^allow vendor_vehicle_logger vendor_vehicle_bus ?: ?unix_stream_socket (\{ ?connectto ?\}|connectto) ?;$")
macro = re.compile(r"^unix_socket_connect\( ?vendor_vehicle_logger ?, ?vehicle_bus ?, ?vendor_vehicle_bus ?\)$")
good = [l for l in extra if rule.match(l) or macro.match(l)]
if not good:
    problems.append("no allow rule for vendor_vehicle_logger -> vendor_vehicle_bus:unix_stream_socket connectto")
for l in extra:
    if l not in good:
        problems.append(f"unexpected extra statement: {l}")
if open("sepolicy/vendor/vehicle_bus.te", encoding="utf-8").read() != BUS_TE:
    problems.append("vehicle_bus.te was changed")
print("\n".join(problems) or "ok")
sys.exit(1 if problems else 0)
