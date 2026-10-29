"""Clara: an offline coding agent for a local model server.

Everything in this package is standard library. The only socket Clara ever
opens on its own is to the model server you configure (loopback by default)
and, when you start `clara serve`, the listening socket for the phone app.
See clara/netguard.py and clara/sandbox.py for how that is enforced.
"""

__version__ = "0.1.0"
