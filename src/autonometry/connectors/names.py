"""Names that connectors read from a robot's network or files, made fit for a sealed log.

Pure and standard library only. A host name, a program name or a file path often holds a
person's name, so a log carries a pseudonym of it, never the name itself (CLAUDE.md product
rule 5); a declared source appears by its ``device_id`` instead.
"""

import hashlib
import re
from typing import Final

#: Hex digits of a pseudonym: 48 bits, enough to tell a site's hosts and programs apart.
PSEUDONYM_CHARS: Final = 12
#: Longest ROS 2 node name written as it is (a fully qualified name, ``/`` namespace included).
MAX_NODE_NAME: Final = 255
# ROS 2 names (design.ros2.org "Topic and Service name mapping"): letters, digits and
# underscores in tokens separated by "/", the first character of each token not a digit.
_NODE_NAME_RE = re.compile(r"/?(?:[A-Za-z_][A-Za-z0-9_]*/)*[A-Za-z_][A-Za-z0-9_]*", re.ASCII)


def pseudonym(name: str | None) -> str | None:
    """The first 12 hex digits of the SHA-256 of ``name``'s UTF-8 bytes (``None`` stays None).
    ``python3 -c "import hashlib; print(hashlib.sha256(b'NAME').hexdigest()[:12])"`` computes the
    same, so a log entry can be traced back to the machine, program or file it stands for."""
    if name is None:
        return None
    return hashlib.sha256(name.encode("utf-8", "replace")).hexdigest()[:PSEUDONYM_CHARS]


def node_name(name: str) -> str:
    """A ROS 2 node name as a log may carry it: the name itself when it follows the ROS 2 name
    rules and has at most :data:`MAX_NODE_NAME` characters, else ``pseudonym:<12 hex>``, so a
    name from the network can carry no control character, no unbounded text and no personal
    data that is not a valid node name."""
    if len(name) <= MAX_NODE_NAME and _NODE_NAME_RE.fullmatch(name):
        return name
    return f"pseudonym:{pseudonym(name)}"
