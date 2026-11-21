# FBVenoison 3.0 — fleet command

FBPoison and the bundled FBVenom run in separate processes, each restricted to
its assigned devices. FBPoison handles reactions; FBVenom includes Comments,
Comment to Comment workspaces, and its own AI settings. The hub uses the current
bundled Python sources, imported unchanged from
[`kaneki654/FBVenom`](https://github.com/kaneki654/FBVenom) (version 1.1.12,
commit `afbfabf0e29ac6d267a3c6e914689a02ac2cc73e`).
The imported app includes the current Venom interface, LAN/server backup, USB
internet, phone-box fixes, diagnostics, and its updater. Explicit executable overrides take priority; packaged
launches otherwise prefer apps shipped beside the hub over legacy home folders.

![Dashboard with a sample fleet](docs/dashboard-preview.png)

## Run

```bash
python fbvenoison.py
```

The existing access key still applies. Install PyQt6 for the hub, and the child
apps' dependencies (including uiautomator2, adbutils and Pillow) for source
launches. Install Android platform-tools and keep `adb` on PATH, or set
`ADB_PATH` to one shared ADB binary for the whole suite.

FBPoison credentials stay in ignored `FBPoison/poison_private.json` beside the
app. Copy `poison_private.example.json` for a fresh installation, or set
`GROQ_API_KEY` and `FBPOISON_UPDATE_TOKEN` in the environment. Private settings,
device captures, logs, and generated binaries are excluded from Git.

Explore the interface without an access key, phones, ADB calls, child launches,
or changes to your saved configuration:

```bash
python fbvenoison.py --demo
```

Preview scanned devices and saved groups without opening the UI or saving:

```bash
python fbvenoison.py --boxes
```

## Devices: 20 / 20, adjustable

The **Device targets** controls default to **20 Poison / 20 Venom**. Set either
target from 0–1000 and click **Rebalance** to apply it. Changing a number saves
the target without moving phones. Rebalance keeps existing ready assignments
where possible, fills vacancies, and leaves surplus ready phones awaiting an
app. If fewer ready phones exist than requested, it shares them proportionally
(30 ready phones for 20/20 gives 15/15). Ready counts exclude offline and
unauthorized devices; their saved destinations remain intact for reconnects.

Select a work section or physical box to see its phones. Select one or more
rows and use **→ Poison**, **→ Venom**, or **Clear app** to move them. Ctrl and
Shift select multiple rows. Individual moves may exceed a target: targets are
planning values, while your manual assignments remain authoritative until you
choose Rebalance again. Search filters the table without changing assignments.

Stop affected apps before moving their phones. Stop both apps before
rebalancing. Every rebalance saves a `boxes.before-realignment-*.json` backup
before committing the new assignment. Failed saves leave the previous live
assignment intact. Targets, manual choices, physical groups, and motion
preferences persist in `boxes.json` (`FBVENOISON_CONFIG` overrides its path).

## Physical phoneboxes

Capture one physical box at a time using **Rescan** or F5. A named box appears
immediately, fills to 20 members, and keeps its name/membership across rescans
and unplugging. A fresh cohort of 20 creates a separate box. ADB cannot infer
physical boxes with no USB information when they first appear simultaneously.

Drag a named box onto a work section to move every member, including currently
disconnected members. With a box selected and no rows selected, the footer's
**Box →** buttons do the same. **Mixed** means members have different app
assignments. Double-click a name or use **Rename** to rename it.

**Split by physical box** routes whole captured boxes alternately to Poison and
Venom. This preserves physical-box boundaries; **Rebalance** instead follows
the requested device counts and may split a physical box between apps.

For a mistaken capture, keep only the intended physical box powered on, select
it, and use **Re-align this box**. The hub backs up the configuration before
replacing that physical group's membership with the current scan (1–20
phones). Ordinary rescans do not discard disconnected members.

## Fusion interface

The generated logo combines a venomous serpent and poison droplet. The login
screen, window icon, and dashboard share it. The generated green-and-violet
liquid background scales to the window and animates with slow drift, light,
and droplets. Use **Motion on/off** to pause it; animation pauses while hidden
and stops repainting while minimized. A resizable sidebar and scrolling
controls support compact windows down to 960 × 720.

Generated image sources and prompts are recorded in
[`assets/branding/SOURCES.md`](assets/branding/SOURCES.md).

## Suite updater

Open **Updates**, enter your publisher's HTTPS manifest URL, then select
**Save & check**. No publisher feed is configured by default. The feed returns:

```json
{
  "version": "3.1.0",
  "url": "https://your-release-host.example/FBVenoison-3.1.0.zip",
  "sha256": "<64 hexadecimal characters from the release package>",
  "notes": "Describe what changed."
}
```

Checks and downloads run in the background. **Download release** stages only a
SHA-256-verified archive or installer in `updates/` beside `boxes.json`;
partial or corrupt downloads are discarded. **Open download folder** opens the
staged package's location. Stop both apps, preserve your local settings, install
the downloaded release, and restart. The updater stages a release for manual
installation; it does not replace or execute files in a running suite.

The feed URL persists in `update_config.json`. See
[`update_config.example.json`](update_config.example.json).
`FBVENOISON_UPDATE_URL` sets the initial feed; `FBVENOISON_UPDATE_DIR` changes
the updater's settings/download location.

## Rebuild and register on Windows

Run this in Windows PowerShell or PowerShell 7:

```powershell
.\rebuild_windows.ps1 -Launch
```

The script **installs or updates regular Python 3.14 x64 first**, using WinGet
or a signature-verified installer from python.org. It selects that interpreter
without relying on a stale PATH, repairs an incompatible `.venv-build`, upgrades
pip/setuptools/wheel, downloads Windows wheels, and installs those wheels
without compiling packages from source. Python 3.15, 32-bit, ARM64 and
free-threaded interpreters are rejected before package installation.

It finds Windows Android platform-tools (or downloads them from Google),
prepares upstream's verified Gnirehtet USB-internet helpers, and rebuilds **FBPoison**, **FBVenom**, and **FBVenoison** into
`release/FBVenoison/`. It packages shared ADB tools with the suite and checks
that all three EXEs and Venom's matching device bridge exist before registering
paths. Close any running app windows before rebuilding their executables.

After successful builds, it saves these **system environment values**:

| Variable | Default value under the project folder |
|---|---|
| `VENOM` | `release\FBVenoison\FBVenom\FBVenom.exe` |
| `POISON` | `release\FBVenoison\FBPoison\fbpoison.exe` |
| `FBVENOISON_VENOM_EXE` | Same path as `VENOM` |
| `FBVENOISON_POISON_EXE` | Same path as `POISON` |
| `FBVENOISON_EXE` | `release\FBVenoison\fbvenoison.exe` |

System registration also refreshes your account's matching values so an older
user-level path cannot shadow the rebuilt apps. System registration requests
Windows administrator approval after building;
Python/build tools run without elevation. Use `-Scope User` to register only
for your account without administrator approval. `-Launch` opens the rebuilt
hub immediately with the new values. Restart an already-open hub to load the
rebuilt applications. FBVenoison recognizes both `VENOM`/`POISON` aliases and
the longer executable variable names, with bundled-file fallback.

```powershell
# Build and register for your account only, then launch.
.\rebuild_windows.ps1 -Scope User -Launch

# Choose Python, tools, and an output folder.
.\rebuild_windows.ps1 -Python 'C:\Python314\python.exe' -PlatformTools 'C:\Android\platform-tools' -OutputDirectory 'C:\Apps\FBVenoison'

# Register an existing completed build without rebuilding.
.\rebuild_windows.ps1 -RegisterOnly -OutputDirectory 'C:\Apps\FBVenoison'

# Build only, without changing persistent environment values.
.\rebuild_windows.ps1 -SkipRegistration
```

`-SkipPythonUpdate` uses an already-installed supported interpreter (useful in
CI). `-SkipDependencyInstall` reuses a valid build environment without installing
packages again. Leave both off when repairing a failed build. Pip logs are saved
under `.build-tools/`. `build_windows.ps1` remains a compatible entry point and forwards
the same options. Existing private settings in the output directory are kept;
only empty example configurations are copied for a fresh installation.

A managed FBVenom build emits `FBVenom.suite-bridge.json` beside its EXE; keep
this matching file with the binary. The standalone upstream 1.1.12 EXE does
not include that bridge, so rebuild before using it through a frozen hub.

All 57 imported files are recorded by SHA-256 in `docs/FBVenom-import.json`.
Upstream source, tests, spec and updater stay byte-for-byte unchanged. The hub's
`venom_bridge.py` and `integration/venom_runtime_hook.py` apply device routing,
shared ADB and the existing hub unlock at launch. USB internet discovery is also
limited to Venom's assigned devices. Rebalance, manual device moves and target
counts remain in FBVenoison; Venom keeps its upstream functions and interface.

`FBVenom-suite.spec` delegates to the unchanged upstream spec and adds the
external runtime hook and a hash-bound capability marker. Upstream's own
update/downgrade functions remain available. Installing a standalone upstream
EXE replaces the managed build, so rebuild the suite before using that EXE for
assigned-device sessions through the hub.

The hub's PyInstaller spec embeds the generated assets and updater. Child apps
remain external processes under `FBPoison/fbpoison.exe` and
`FBVenom/FBVenom.exe`; this keeps their identically named `core`/`gui` modules
isolated. The source bootstrap filters each child's `core.list_devices()`.

## Verify

```bash
python tests/run_all.py
```

Tests use temporary settings and fake ADB fleets. They cover flexible targets,
manual moves, physical capture/realignment, drag/drop, isolated child launches,
packaged app resolution, authentication, and verified updater downloads. The
bundled FBVenom also has workspace and live-counting tests in its own folder.
