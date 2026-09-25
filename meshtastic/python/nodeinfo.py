"""Phase 6: telling the mesh who this node is.

Meshtastic nodes learn each other's names from one payload type, `NodeInfo`,
carrying a `User` message. Until a node sends one it appears in everyone else's
list as a bare number, which is why early captures here were full of them.
Sending one is the smallest piece of mesh citizenship and the only one
that cannot inconvenience anybody: it is a single broadcast that answers a
question every receiver already has.

The encoder is here rather than in Rust on purpose, and it is the other half of
a rule the crate already follows. Rust parses the envelope because that is
hostile input from the air, where a length field can lie. This is our own
message, built from our own settings, so nothing about it is untrusted, and
proto3 encoding of four known fields is a dozen lines that would cost a rebuild
of the .mpy every time a field was added. The encoding itself is in
`mesh_proto`, which is shared with the phone API.

Field numbers are from `User` in mesh.proto at the pinned tag, cross-checked
against the decoder in meshtastic.py, which has been reading them off the air
since Phase 3.
"""

import supervisor

from meshtastic import meshlib as keystore
from meshtastic import meshlib as pb
from meshtastic import meshlib as mt

#: PortNum.NODEINFO_APP.
PORT_NODEINFO = 4

#: From UserLite in deviceonly.options. Longer names are accepted by the wire
#: format but truncated by every node that stores them, so truncate here where
#: it is visible rather than letting each receiver do it differently.
MAX_LONG_NAME = 40
MAX_SHORT_NAME = 5

#: DeviceRole.CLIENT. This node listens and occasionally speaks; it does not
#: rebroadcast, and claiming a routing role while not routing would be a lie
#: that costs other people delivery.
ROLE_CLIENT = 0


def default_names(node_num):
    """What a node calls itself before anyone renames it.

    The firmware builds these from the last two bytes of the MAC; the last two
    of the node number are the same bytes, since that is where it came from.
    Byte for byte what `NodeDB::installDefaultDeviceState` writes -- lower case
    and four digits -- so a node of ours that has never been named is
    indistinguishable from a stock one that has never been named.
    """
    tail = "%04x" % (node_num & 0xFFFF)
    return "Meshtastic %s" % tail, tail


def names(node_num):
    """This node's names: renamed, configured, or derived, in that order.

    NVM first, because that is where a rename from the phone lands and a name
    someone typed into the app should survive the reboot that follows it.
    Then settings.toml, which is this tree's answer to the firmware's
    USERPREFS_CONFIG_OWNER_* build flags -- a name baked into the image before
    anyone has touched it. Then the derived pair, which is what stock does.

    The consequence worth knowing: once the phone has set a name, editing
    settings.toml no longer changes anything. `configure(long_name=None)`
    clears the NVM record and hands the file back its say.
    """
    long_name, short_name = default_names(node_num)
    return (keystore.text(keystore.LONG_NAME)
            or supervisor.get_setting("MESH_LONG_NAME", "")
            or long_name,
            keystore.text(keystore.SHORT_NAME)
            or supervisor.get_setting("MESH_SHORT_NAME", "")
            or short_name)


def user(node_num, long_name, short_name, hw_model=None,
         role=ROLE_CLIENT):
    """A `User` message: who we are, in the form other nodes store."""
    if hw_model is None:
        # Read now rather than bound as a default: the adapter sets it at
        # start(), which is long after this module is imported.
        hw_model = mt.HW_MODEL
    buf = pb.msg_buffer()
    at = pb.string(buf, 0, 1, "!%08x" % node_num, 16)
    at = pb.string(buf, at, 2, long_name, MAX_LONG_NAME)
    at = pb.string(buf, at, 3, short_name, MAX_SHORT_NAME)
    at = pb.uint(buf, at, 5, hw_model)
    at = pb.uint(buf, at, 7, role)
    return pb.msg_take(buf, at)
