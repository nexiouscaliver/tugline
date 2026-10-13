"""Claude Code adapter for tugline.

Modules here talk to Claude Code on one side (hook payloads on stdin,
hook entries in the settings JSON) and to the tugline core on the
other (spool records, policy state, client config). Nothing in this
package reaches the network; the daemon owns publishing.
"""
