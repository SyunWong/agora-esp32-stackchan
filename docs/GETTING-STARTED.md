# Build guide

This guide is for a **new exported recipe checkout**, not the author's live
experiment tree. The tools below prepare configuration, create a dedicated Worker, and back up
and install a supported robot. Their new deployment/install paths have offline
failure-injection coverage; a fresh-account and second-device end-to-end run
remains a release-validation gate. This is still a developer preview.

## 1. Prepare your tools and source

The tested project used ESP-IDF 6.0.3, Board Manager 0.7.6, `m5stack_boards` 0.6.2
and `esp-bmgr-assist` 0.8.4. Use the official ESP-IDF installation procedure and
activate its environment. The recipe also uses Git, Python 3.10+, a C compiler for the configuration
regression check, and a modern browser.
The dependency manifest pins the M5Stack profile. The exported dependency lock
records the versions resolved by the working project; preserve it and review
any changes produced by your build. A fresh-environment build still needs validation.

From the recipe root:

```sh
git clone https://github.com/espressif/esp-webrtc-solution.git esp-webrtc-solution
git -C esp-webrtc-solution checkout dc3ab928137916d7b214a9a8ffe52e0e81794471
git -C esp-webrtc-solution submodule update --init --recursive
git -C esp-webrtc-solution apply --check ../firmware/stackchan-media.patch
git -C esp-webrtc-solution apply ../firmware/stackchan-media.patch
cp firmware/dependencies.lock esp-webrtc-solution/solutions/agora_demo/dependencies.lock
python3 tools/check_setup.py
python3 tools/check_release_tools.py
python3 -m unittest discover -s cloud/tests
python3 tools/fetch_web_sdk.py
python3 tools/check_package.py
```

The patch preserves the project's recorded media snapshot. It deliberately does
not include the pending HTTP connection-reuse experiment or later volume tuning.
The optional `firmware/stackchan-battery.patch` can be applied on top after the
base experience works; it is not required for the two voice milestones.

## 2. Prepare your cloud configuration

Choose an HTTPS hostname you control, for example `https://robot.example.com`.
The placeholder is not a hosted demo. Use an Agora App ID and App Certificate
from the same project, with Conversational AI access. Confirm that your LLM/TTS
accounts and chosen Fish Audio voice are usable before attempting a device call.

```sh
python3 tools/configure.py cloud --origin https://robot.example.com
```

The tool prompts with hidden input for Agora credentials, DeepSeek and Fish Audio
keys, and the website password. It generates a new device key and writes
`private/bindings.json` with owner-only permissions. It starts with `ENABLED=false`,
refuses to overwrite existing configuration, prints no secrets, and performs no
network requests. Do not put these values into the HTML, source control or screenshots.

The current Worker profile implements the existing Deepgram-managed recognition
route and DeepSeek/Fish Audio configuration. This recipe does not imply that a
separate Deepgram key is needed by that route or that every provider is selectable
through environment variables. Provider changes require code/schema validation.

## 3. Assemble and deploy your own control service

```sh
python3 tools/bundle_cloud.py
```

This generates `build/cloud/modules.json` and `release.txt`. It checks the browser
SDK hash and includes only named Python modules and static assets. It does not
upload anything. The JSON is a multipart-upload manifest, **not** a Wrangler config.

The new first-deployment tool uses Cloudflare's Workers multipart API. It
uploads the code and six Secrets together, creates the `StackChan` SQLite
Durable Object with migration `v1`, and enables the service. It retains the
project's tested `2026-11-26` compatibility date and Python flags. It enables
sampled Worker logs/traces; application code does not log tokens or transcripts.

Choose an unused Worker name in your own account. Replace the account ID below
with your 32-character Cloudflare account ID. First review an **offline plan**:

```sh
python3 tools/deploy_cloud.py --account-id YOUR_ACCOUNT_ID --name my-stackchan
```

To create the service, run the same command with `--apply`:

```sh
python3 tools/deploy_cloud.py --account-id YOUR_ACCOUNT_ID --name my-stackchan --apply
```

Only `--apply` contacts Cloudflare. It prompts privately for an API token with
Workers Scripts Edit permission scoped to that account; it does not save the
token. Provider keys and the device/password credentials come from the private
configuration. It rebuilds the bundle before upload, checks the account's Worker
inventory, and refuses existing names or incomplete inventory results. Do not
run concurrent deployments to the same name. This is a **new-Worker tool**, not
an updater for an existing deployment.

In Cloudflare's dashboard, open the created Worker → Settings → Domains & Routes
and add the custom domain chosen in step 2. Use an available hostname in a zone
you control; do not replace another service's DNS. Wait for its HTTPS certificate
and routing to become ready. The tool deliberately leaves DNS/domain changes
for this explicit dashboard step.

An attempt receipt is saved under `private/deploy-WORKER-NAME.json`. A timeout
can happen after successful creation, so the uploader never automatically retries
or overwrites its receipt. Inspect the account before retrying. If a Worker
exists, keep its Secrets and namespace intact and use a separately reviewed
upgrade procedure. If it definitely does not exist, archive the failed receipt
before another attempt.

API reference: [Upload Worker](https://developers.cloudflare.com/api/resources/workers/subresources/scripts/methods/update/).
Fresh-account deployment and runtime validation remain pending; offline request
checks are not a claim of live deployment success.

Verify `/healthz`, `/auth/config`, password login and authenticated status. Wrong
passwords and unauthenticated calls must be rejected. Device offline is expected
until the firmware connects. None of these checks should place a call automatically.

The author's network used a separate fixed-target device ingress relay. It only
forwards five control routes; audio still goes directly to Agora. That deployment
is not a shared service for readers. Direct HTTPS control on a new hostname and
any optional relay need device-side connectivity validation before release.

## 4. Configure and build the robot

Install the tested board helper into your activated IDF Python environment, then
generate the board profile:

```sh
python -m pip install esp-bmgr-assist==0.8.4
cd esp-webrtc-solution/solutions/agora_demo
idf.py bmgr -b m5stack_cores3
cd ../../..
python3 tools/configure.py firmware --control-origin https://robot.example.com
cd esp-webrtc-solution/solutions/agora_demo
idf.py -D IDF_TARGET=esp32s3 build
cd ../../..
```

The firmware tool prompts for Wi-Fi credentials, reuses the device key, and creates
an ignored `stackchan_session_local.h`. It selects interactive AI, the display,
DIO flash mode and **disabled head motion**. It refuses to replace an existing
header or sdkconfig. If the board-generation command creates sdkconfig, review
and move that credential-free generated file aside before configuration; never
delete a populated setup blindly.

Do not enter the Agora App Certificate in the upstream Bot Station menu. This
application obtains short-lived media grants from the cloud service instead.
A build writes the candidate image to `esp-webrtc-solution/solutions/agora_demo/build/`.
That image contains device/Wi-Fi credentials: do not publish it.

## 5. Install only after verifying your device layout

Use the active ESP-IDF Python environment, which supplies `esptool` and
`pyserial`. Attach the **head's side USB-C data port**. Close serial monitors and
finish any active calls; backup and flashing reset the device. List ports to
find your robot's USB hardware serial identity:

```sh
python -m serial.tools.list_ports -v
```

Replace `YOUR_USB_IDENTITY` below with its 12 hexadecimal digits (colon-separated
format is also accepted). This is not a `/dev/cu...` path and not the author's ID.
Create a new private backup directory:

```sh
python tools/install_interactive.py backup --usb-identity YOUR_USB_IDENTITY --backup-dir private/device-backup
```

Backup never writes flash. It checks ESP32-S3/16 MiB flash and requires disabled
secure boot/flash encryption, then reads the entire flash and records its hash
and hardware identity. It refuses to overwrite an existing backup. Unsupported
layouts still retain the backup but cannot proceed to installation.

The installer supports only the documented StackChan layout: `ota_0` at
`0x20000`, capacity `0x4f0000`, with the exact expected NVS/OTA/assets partitions
and a valid partition-table checksum. It requires OTA selection of `ota_0` and
rejects pending/ambiguous OTA state. It never switches slots or writes the
bootloader, partition table, NVS, calibration or resources. A different layout
requires a separate hardware port, not edited validation constants.

Review the candidate offline first:

```sh
python tools/install_interactive.py install --usb-identity YOUR_USB_IDENTITY --backup-dir private/device-backup --candidate esp-webrtc-solution/solutions/agora_demo/build/agora_demo.bin
```

The same command with `--apply` performs live identity, security, bootloader,
partition and OTA-selection checks, saves the app currently on the device, writes
only `ota_0`, and verifies the write:

```sh
python tools/install_interactive.py install --usb-identity YOUR_USB_IDENTITY --backup-dir private/device-backup --candidate esp-webrtc-solution/solutions/agora_demo/build/agora_demo.bin --apply
```

If writing or verification fails, it attempts to restore and verify the app that
was present immediately before the attempt. If recovery cannot finish, keep USB
connected and inspect `private/install-*/result.json`; do not claim the device
was restored. Retain the full backup even after success.

To explicitly return to the original backup app, first run `restore` without
`--apply` to inspect the plan, then repeat with `--apply`:

```sh
python tools/install_interactive.py restore --usb-identity YOUR_USB_IDENTITY --backup-dir private/device-backup
```

This restores the backup's application, not its NVS or other partitions. Never
run generic `idf.py flash` or `erase-flash` for this adaptation. The portable
installer's safety/failure paths have offline tests; a second-device physical
installation is still pending. Keep head motion disabled during that validation.

After a reviewed app-only install and verification, wait for the idle face and
online status, then complete [both listening milestones](TRY-IT.md). Keep
head motion disabled during this first build. Record any changes to firmware,
providers, gain, network or hardware alongside the results.
