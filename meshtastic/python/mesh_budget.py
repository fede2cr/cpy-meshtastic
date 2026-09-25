"""What the board underneath can afford, read from settings.toml at import.

Every other size in this library is chosen by the mesh: a roster holds what a
mesh produces, an inbox holds what strangers type, the phone queue holds what
the app has to be told before it will finish connecting. This module is the
other constraint, and it is separate because the two move independently. An
nRF52840 has 192 kB of RAM once the SoftDevice has taken its share, and by the
time the phone service is up there is a single-digit number of kilobytes of it
left; an ESP32-S3 with eight megabytes of PSRAM should not be held to the same
numbers, and neither should be guessing at the other's.

**Not NVM, unlike `mesh_config`.** Those settings are the node's, and a node
retunes itself, so they have to be writable from the board. How much RAM the
board has is not something it can change its mind about, and a budget the phone
could edit is a budget the phone could edit into a board that no longer boots.
settings.toml is a file on the drive, read by the supervisor before any of this
runs and editable only from a host -- which is exactly the failure mode these
numbers deserve, because the way out of a bad one has to work on a board that
cannot get far enough to offer a prompt.

Every default here is the generous answer, so a board that says nothing gets
the whole library. The small numbers live in the settings.toml that ships
beside it, next to a note saying which board they were measured on.
"""

import supervisor


def budget_setting(key, default):
    """One settings.toml key, coerced to the type of `default`.

    Nothing here is worth failing a boot over. A missing settings.toml, a
    firmware built without support for one, a typo where a number belongs: each
    of them lands on the default, because a node that comes up with the wrong
    inbox size can be told so, and one that does not come up cannot.
    """
    try:
        value = supervisor.get_setting(key, default)
    except (AttributeError, NotImplementedError, ValueError):
        return default
    # Before the int check, because a bool is one: `MESH_CACHE = 1` should read
    # as "on" rather than as a size of one byte.
    if isinstance(default, bool):
        return bool(value)
    if isinstance(default, int) and not isinstance(value, int):
        print("# budget: %s is not a whole number, using %d" % (key, default))
        return default
    return value
