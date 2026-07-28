# Database lifecycle: stopping the auto-start at login

PartGraph's database can be started by **two** independent owners: Compose (via
`partgraph db up`) and — on hosts where someone generated one — a
quadlet-generated `systemd --user` unit named `partgraph-dgraph.service`.

`partgraph db down` stops both (ADR-0021). What it cannot do is stop the second
owner from starting the database **again at your next login**, because that
behaviour lives in a unit file on your host, outside this repository. On the
host this document was written for, the measured cost of that was a single
PartGraph instance idling for **14 h 10 m**, at **10.2 GB** peak memory, that
nobody had asked for.

This document is the procedure for removing it. **You run it; PartGraph never
does.** The directory involved is shared with unrelated projects' units, so
this repository will not write into it, will not run `systemctl` for anything
except read-only queries, and ships a read-only diagnostic instead:

```console
$ partgraph db doctor
```

`db doctor` reports what is running, whether the unit exists and will start at
login, whether the data volume exists, and these same steps. It changes
nothing, and it **always exits 0** — it is a report, not a health check. Use
`partgraph db status` when you want an exit code that means something.

### Which engine this applies to

**Everything below is Podman-specific.** The second lifecycle owner it removes
is a *quadlet* unit, and quadlet is a Podman feature: Podman generates the
`systemd --user` units from a `.container` file
(`podman-systemd.unit(5)` — "systemd units using Podman Quadlet"). On a host
running Docker instead, that unit cannot exist, no `systemctl --user` step here
has anything to act on, and Section 1 is not a procedure you are missing —
there is simply nothing to remove.

A Docker host can still end up with a database that keeps coming back, but by
an unrelated route: the Docker daemon enforces the container's own `restart`
policy, which is why `docker/docker-compose.yml` sets `restart: "no"` and
[ADR-0022](decisions/ADR-0022-database-lifecycle-on-demand.md) § 7f explains
that value for both engines separately. If your database returns after a
reboot on Docker, look at that policy — not for a unit file. *(That Docker
behaviour is taken from Docker's documentation; this repository has only ever
been run against rootless Podman, so it is not something anyone here has
observed. The incident, the measurements and the procedure below are all from a
Podman host.)*

---

## 1. Remove the autostart: `WantedBy=`

Quadlet units are **generated** at boot from a `.container` file, so
`systemctl --user disable` does not work on them (podman-systemd.unit(5)); there
is no persistent enablement symlink to delete. The only documented way to remove
autostart is to drop the `WantedBy=default.target` value the generated unit
carries — and the safe way to do that is a **drop-in override**, never a direct
edit of the generated file.

The quadlet source file for this project is
`partgraph-dgraph.container`, in your own systemd user directory
(`$HOME/.config/containers/systemd/`, which systemd itself writes as `%h`):

```console
$ mkdir -p "$HOME/.config/containers/systemd/partgraph-dgraph.container.d"
$ cat > "$HOME/.config/containers/systemd/partgraph-dgraph.container.d/override.conf" <<'EOF'
[Install]
# An empty assignment RESETS the list systemd inherited from the generated
# unit, rather than adding to it. Without this line the generated
# WantedBy=default.target still applies.
WantedBy=
EOF
$ systemctl --user daemon-reload
```

> **Edit only `partgraph-dgraph.container`, and only inside the
> `partgraph-dgraph.container.d/` drop-in directory you just created.** That
> systemd directory is shared: on the host this was written for it also holds
> `cve-alpha`, `cve-loader`, `cve-ratel`, `cve-zero` and `min-web` units, which
> belong to other projects entirely. Nothing in this document applies to them,
> and removing autostart from the wrong unit will stop somebody else's service.
> If a file there is not named `partgraph-dgraph.*`, do not touch it.

Verify it took effect — the same read-only command as before:

```console
$ partgraph db doctor
```

The `Autostart at login (WantedBy=default.target):` line should no longer say
`yes`. If it says `unknown`, systemd gave no evidence either way; check the unit
by hand with `systemctl --user show partgraph-dgraph.service --property=WantedBy`.

Stopping autostart does **not** stop a database that is running right now. Run
`partgraph db down` for that; the named data volume is never removed.

To undo this, delete the `override.conf` you created (or the whole
`partgraph-dgraph.container.d/` directory) and run `systemctl --user
daemon-reload` again.

---

## 2. Optional: the unit's stop budget, `StopTimeout=` — and what it does not fix

If the unit takes a long time to stop and ends up killed, you can raise its stop
budget with a second drop-in, in the same
`partgraph-dgraph.container.d/` directory (again: a drop-in, never an edit of
the generated file):

```console
$ cat > "$HOME/.config/containers/systemd/partgraph-dgraph.container.d/stop-timeout.conf" <<'EOF'
[Service]
StopTimeout=90
EOF
$ systemctl --user daemon-reload
```

**Read this before you bother.** Raising that number only lengthens the wait; it
does not make the shutdown graceful, and on its own it fixes nothing.

The `dgraph/standalone` image declares no `ENTRYPOINT` and no `STOPSIGNAL`, and
its `/run.sh` starts `dgraph zero &` and then runs `dgraph alpha` in the
foreground under `bash`. PID 1 is therefore bash, which defers signals until its
foreground command exits — so **SIGTERM never reaches Dgraph at all**, no matter
how long anything waits for it. This was measured, not assumed: a container up
for barely one minute, with essentially nothing to flush, still burned a full
60-second budget and exited 137 (SIGKILL).

The real fix is a proper init process at PID 1. PartGraph applies it with
`init: true` in `docker/docker-compose.yml`, and the same stop then completes in
0.2 s with exit 143 — a genuine SIGTERM exit. **That fix does not reach the
quadlet path**: the unit runs `podman run` directly and never goes through
Compose, so a quadlet-started container still gets bash as PID 1, still will not
be delivered SIGTERM, and is still SIGKILLed at the end of whatever budget you
set. A `StopTimeout=` drop-in buys you a longer wait for the same kill, and
nothing else.

If you want the graceful path on this host, do not raise the timeout: stop using
the quadlet unit (Section 1), and start the database with `partgraph db up`,
which goes through Compose and gets the init process.

No data is at risk either way. Badger's write-ahead log means the earlier
SIGKILLs cost nothing — 613,396 `Part` nodes were verified intact afterwards.
This is about shutdown correctness and restart cost, not integrity.

---

## What PartGraph will and will not do

| | |
| --- | --- |
| **Documents** the procedure above | this file |
| **Detects and reports** the state | `partgraph db doctor` (read-only, always exits 0) |
| **Stops** every running lifecycle owner | `partgraph db down` |
| **Never** writes into your systemd user directory | enforced by a static scan over `src/`, not by prose |
| **Never** runs `systemctl --user daemon-reload` itself | same scan |

The reasoning behind all of it is in
[ADR-0022](decisions/ADR-0022-database-lifecycle-on-demand.md), and the selector
policy `db doctor` reports through is in
[ADR-0021](decisions/ADR-0021-db-down-all-lifecycle-owners.md).
