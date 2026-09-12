# 0007a - Container measurement run for I13 (#79)

Status pending. Depends on docs/design/decisions/0007-load-profile.md. Tool
docs/design/decisions/0007-load-profile.md (fill after I13-04/05 land:
tools/measure_containers.py collect / summarize). Compose project dxp (default,
ORCHESTRATOR confirms the real name post-I6).

#65 (I6) has not landed as of this writing, so no measurement has been taken. This
file is the skeleton the day-long run will fill. The source file uses the actual
multiplication-sign and en-dash glyphs; these tables render them as ASCII x and -.

## 4. The profile at 1x

| | CPU, cores | Memory | Disk I/O | Network | Bound by |
|---|---|---|---|---|---|
| `feed` | pending | pending | pending | pending | pending |
| `store` | pending | pending | pending | pending | pending |
| `api` | pending | pending | pending | pending | pending |
| `web` | pending | pending | pending | pending | pending |
| Redis | pending | pending | pending | pending | pending |

## 5. The profile at 10x

| | CPU, cores | Memory | Disk I/O | Network | Bound by |
|---|---|---|---|---|---|
| `feed` | pending | pending | pending | pending | pending |
| `store` | pending | pending | pending | pending | pending |
| `api` | pending | pending | pending | pending | pending |
| `web` | pending | pending | pending | pending | pending |
| Redis | pending | pending | pending | pending | pending |

## Start-to-healthy per container

One row per container the Compose project reports, filled in when the run completes.

| Container | started_at | healthy_at (or first-seen-running if the container defines no HEALTHCHECK) | seconds |
|---|---|---|---|
| pending | pending | pending | pending |

## Cells that moved more than 25%

pending - no measurement has been taken yet.

## Method

The tool is tools/measure_containers.py and its two entry points are collect and
summarize. The sampling interval is SAMPLE_INTERVAL_SECONDS, proposed 5 seconds and
argued in the plan's unsettled_decisions. Container names come from querying the
Docker daemon for the Compose project rather than a fixed list.

Filling this table is a transcription of tools/measure_containers.py summarize's
output, not a design decision, per R1-R7.
